import asyncio
import hashlib
import logging
import os
from pathlib import Path
import random
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

load_dotenv()

# Silence verbose third-party HTTP request logs (httpx/httpcore)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from pymongo.errors import BulkWriteError, DuplicateKeyError

import db
import monitoring
import pangram
import rate_limit
from auth import current_participant
import admin


# ─────────────────────────── App lifecycle ───────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    export_task = None
    monitor_task = None
    # Hourly export runs automatically in the background by default (no manual action needed).
    export_enabled = os.environ.get("ENABLE_HOURLY_EXPORT", "true").lower() not in ("false", "0", "no", "off")
    if export_enabled:
        import hourly_export
        interval = int(os.environ.get("EXPORT_INTERVAL_SECONDS", "3600"))
        zip_target = Path(__file__).resolve().parent / "exports.zip"
        export_task = asyncio.create_task(
            hourly_export.run_daemon_for_db(
                db_getter=lambda: db.state.db,
                out_path=zip_target,
                interval_seconds=interval,
                initial_delay=5.0,
            )
        )
    # Background phone alert monitor (fires via ntfy.sh when MongoDB crashes)
    import alerts
    monitor_interval = int(os.environ.get("ALERT_INTERVAL_SECONDS", "30"))
    monitor_task = asyncio.create_task(
        alerts.run_mongo_health_monitor(interval_seconds=monitor_interval)
    )
    try:
        yield
    finally:
        if monitor_task:
            monitor_task.cancel()
            try:
                await monitor_task
            except (asyncio.CancelledError, Exception):
                pass
        if export_task:
            export_task.cancel()
            try:
                await export_task
            except (asyncio.CancelledError, Exception):
                pass
        await db.disconnect()


app = FastAPI(title="SAIR Backend", version="0.1.0", lifespan=lifespan)

# CORS: open for Phase 1 (local dev with curl + extension). Tighten before
# deploying to a public hostname.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory traffic & latency monitoring middleware (zero DB overhead)
@app.middleware("http")
async def monitor_requests_middleware(request: Request, call_next):
    start = time.perf_counter()
    monitoring.record_in_flight_start()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    except Exception:
        status_code = 500
        raise
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        client_ip = _client_ip(request)
        monitoring.record_request(
            method=request.method,
            path=request.url.path,
            status_code=status_code,
            duration_ms=duration_ms,
            client_ip=client_ip,
        )
        monitoring.record_in_flight_end()

# Researcher dashboard — all routes live under /admin/* and are self-contained
# in the admin/ package. Auth is via a single shared password (ADMIN_SECRET).
admin.setup(app)

from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
from fastapi.responses import HTMLResponse, JSONResponse

@app.exception_handler(ServerSelectionTimeoutError)
@app.exception_handler(ConnectionFailure)
async def mongo_connection_exception_handler(request: Request, exc: Exception):
    logging.getLogger("uvicorn.error").warning("MongoDB connection failure intercepted: %s", exc)
    if request.url.path.startswith("/admin"):
        return HTMLResponse(
            status_code=503,
            content="""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"><title>Database Offline - SAIR Admin</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-zinc-950 text-zinc-100 flex items-center justify-center min-h-screen p-4 font-sans">
  <div class="max-w-md w-full bg-zinc-900 border border-zinc-800 rounded-2xl p-6 shadow-2xl">
    <div class="flex items-center gap-3 text-rose-500 mb-3">
      <span class="p-2 rounded-xl bg-rose-500/10 border border-rose-500/20 text-2xl">⚠️</span>
      <div>
        <h1 class="text-base font-semibold text-zinc-100">MongoDB Disconnected</h1>
        <p class="text-[11px] text-zinc-500">SAIR Admin Portal is Running</p>
      </div>
    </div>
    <p class="text-xs text-zinc-300 leading-relaxed mb-4">
      The backend is active, but MongoDB is not reachable (connection refused or timed out).
    </p>
    <div class="bg-black/60 border border-zinc-800 rounded-lg p-3 mb-5">
      <div class="text-[11px] text-zinc-400 mb-1.5 font-medium">To restart MongoDB on server:</div>
      <code class="text-xs text-rose-300 font-mono select-all">sudo systemctl restart mongod</code>
    </div>
    <div class="flex items-center justify-between">
      <a href="/admin/" class="px-4 py-2 bg-zinc-800 hover:bg-zinc-700 text-xs font-semibold rounded-lg text-zinc-200 transition-colors">
        Go to Admin Overview
      </a>
      <a href="/admin/mets" class="text-xs text-zinc-400 hover:text-zinc-200 transition-colors">
        View System Metrics &rarr;
      </a>
    </div>
  </div>
</body>
</html>
            """,
        )
    return JSONResponse(
        status_code=503,
        content={"detail": "Database unavailable: MongoDB is disconnected or timed out. Please check server status."},
    )


# ─────────────────────────── Models ───────────────────────────

class Engagement(BaseModel):
    likes: int | None = None
    retweets: int | None = None
    replies: int | None = None
    views: int | None = None
    comments: int | None = None
    reposts: int | None = None


class ScoreRequest(BaseModel):
    platform: str = Field(..., pattern="^(twitter|linkedin)$")
    postId: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)


class ScoreResponse(BaseModel):
    cached: bool
    source: str
    detector: dict[str, Any]


class ObservationIn(BaseModel):
    platform: str = Field(..., pattern="^(twitter|linkedin)$")
    postId: str = Field(..., min_length=1)
    url: str | None = None
    author: str | None = None
    # Author metadata — populated where the platform exposes it. Twitter posts
    # carry displayName + verified + verifiedType; LinkedIn posts carry
    # displayName + headline + connectionDegree. Missing fields stay null.
    authorDisplayName: str | None = None
    authorVerified: bool | None = None
    authorVerifiedType: str | None = None
    authorPremium: bool | None = None
    authorHeadline: str | None = None
    authorConnectionDegree: str | None = None
    isPromoted: bool | None = None
    # postedAt is an ISO timestamp when the platform exposes one (Twitter
    # via the <time datetime> attribute); LinkedIn only exposes a relative
    # form which we store separately.
    postedAt: str | None = None
    postedAtRelative: str | None = None
    engagement: Engagement | None = None
    seenAt: int | None = None
    badgeShown: bool | None = None
    dwellMs: int | None = 0
    dwellCount: int | None = 0


class InteractionIn(BaseModel):
    platform: str = Field(..., pattern="^(twitter|linkedin)$")
    postId: str = Field(..., min_length=1)
    # Free string so we can add new action types without schema changes; the
    # client side controls the vocabulary (currently like_click, comment_click,
    # repost_click, send_click).
    action: str = Field(..., min_length=1, max_length=64)
    occurredAt: int | None = None


class DwellItem(BaseModel):
    platform: str = Field(..., pattern="^(twitter|linkedin)$")
    postId: str = Field(..., min_length=1)
    dwellMs: int = Field(..., ge=1, le=86400000)
    occurredAt: int | None = None


class DwellIn(BaseModel):
    events: list[DwellItem] | DwellItem



# ─────────────────────────── Helpers ───────────────────────────

def _hash_text(text: str) -> str:
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


def _min_text_length() -> int:
    try:
        return int(os.environ.get("MIN_TEXT_LENGTH", "250"))
    except ValueError:
        return 250


def _participant_id(participant: dict) -> str:
    return participant.get("participantId") or str(participant["_id"])


def _stringify_ids(doc: dict) -> dict:
    if "_id" in doc:
        doc["_id"] = str(doc["_id"])
    return doc


# ─────────────────────────── Endpoints ───────────────────────────

def _client_ip(request: Request) -> str:
    # When the backend sits behind Apache (Phase 4) the real client IP arrives
    # in X-Forwarded-For. Until then, request.client.host is the direct peer.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RegisterRequest(BaseModel):
    externalId: str | None = None
    timezone: str | None = None


@app.post("/register")
async def register(request: Request) -> dict:
    """Register or re-register a participant.

    If an `externalId` (e.g. Prolific PID) is provided and a participant with
    that ID already exists, this is treated as a **re-install**: a fresh token
    is issued (the old token is invalidated) but the original participantId and
    group assignment are preserved.  This ensures that uninstall→reinstall with
    the same recruitment-platform ID always lands the participant in the same
    study group.

    If the externalId has never been seen before (or no externalId is given),
    a brand-new participant row is created with a randomly assigned group.  The
    IP-based rate limit only applies to new registrations — returning
    participants bypassing re-registration do not consume a rate-limit slot.
    """
    external_id: str | None = None
    client_tz: str | None = None
    try:
        raw = await request.body()
        if raw:
            payload = RegisterRequest.model_validate_json(raw)
            external_id = (payload.externalId or "").strip() or None
            client_tz = (payload.timezone or "").strip() or None
    except Exception:
        external_id = None
        client_tz = None

    now = datetime.now(timezone.utc)

    # ── Re-registration path: externalId already exists ──────────────────────
    # Check before rate-limiting so returning participants aren't penalised.
    current_cohort = int(os.environ.get("CURRENT_COHORT", os.environ.get("CURRENT_ROUND", 1)))

    if external_id:
        # Check if participant is in blocklist
        block_entry = await db.state.db.blocked_participants.find_one({"identifier": external_id})
        existing = await db.state.db.participants.find_one({"externalId": external_id})
        if block_entry or (existing and existing.get("blocked")):
            if existing and not existing.get("blocked"):
                await db.state.db.participants.update_one(
                    {"_id": existing["_id"]},
                    {"$set": {"blocked": True, "blockedReason": (block_entry and block_entry.get("reason")) or "Blocked"}}
                )
            raise HTTPException(
                status_code=403,
                detail="Participant is blocked from this study.",
            )

        if existing:
            # Rotate the token so the old install can no longer make requests.
            new_token = secrets.token_urlsafe(32)
            update_set: dict[str, Any] = {
                "token": new_token,
                "lastReRegisteredAt": now,
            }
            if "enrolledAt" not in existing:
                update_set["enrolledAt"] = now
            if "cohort" not in existing:
                update_set["cohort"] = existing.get("round", current_cohort)
            if client_tz:
                update_set["timezone"] = client_tz
            await db.state.db.participants.update_one(
                {"_id": existing["_id"]},
                {
                    "$set": update_set,
                    "$inc": {"reRegisterCount": 1},
                },
            )
            return {
                "participantId": existing["participantId"],
                "token": new_token,
                "group": existing["group"],
                "cohort": existing.get("cohort", existing.get("round", current_cohort)),
                "createdAt": existing["createdAt"].isoformat()
                    if hasattr(existing.get("createdAt"), "isoformat")
                    else str(existing.get("createdAt", "")),
                "externalId": external_id,
                "reRegistered": True,
            }

    # ── New registration path ─────────────────────────────────────────────────
    # Apply IP rate limit only for genuinely new participant registrations.
    try:
        rate_limit.check_and_record_register(_client_ip(request))
    except rate_limit.RateLimitExceeded as e:
        raise HTTPException(
            status_code=429,
            detail=str(e),
            headers={"Retry-After": str(e.retry_after)},
        )

    participant_id = f"u-{uuid.uuid4().hex[:16]}"
    token = secrets.token_urlsafe(32)

    # Randomly assign participant to control or treatment group.
    # Control-group users see no AI/Human/Mixed badges in-browser; all
    # other behaviour (scoring, observation logging) is identical.
    p = float(os.environ.get("CONTROL_GROUP_PROBABILITY", "0.5"))
    group = "control" if random.random() < p else "treatment"

    doc: dict[str, Any] = {
        "participantId": participant_id,
        "token": token,
        "role": "participant",
        "group": group,
        "cohort": current_cohort,
        "timezone": client_tz or "America/New_York",
        "active": True,
        "createdAt": now,
        "enrolledAt": now,
        "source": "self-register",
        "reRegisterCount": 0,
    }
    if external_id:
        doc["externalId"] = external_id
    try:
        await db.state.db.participants.insert_one(doc)
    except DuplicateKeyError:
        # Race condition: another request inserted a participant with the same
        # externalId between our find_one and this insert. Look up the winner
        # and treat it as a re-registration (rotate their token).
        existing = await db.state.db.participants.find_one({"externalId": external_id})
        if existing:
            new_token = secrets.token_urlsafe(32)
            await db.state.db.participants.update_one(
                {"_id": existing["_id"]},
                {"$set": {"token": new_token, "lastReRegisteredAt": now},
                 "$inc": {"reRegisterCount": 1}},
            )
            return {
                "participantId": existing["participantId"],
                "token": new_token,
                "group": existing["group"],
                "createdAt": existing["createdAt"].isoformat()
                    if hasattr(existing.get("createdAt"), "isoformat")
                    else str(existing.get("createdAt", "")),
                "externalId": external_id,
                "reRegistered": True,
            }
        raise  # unexpected duplicate (on token or participantId) — re-raise
    return {
        "participantId": participant_id,
        "token": token,
        "group": group,
        "cohort": current_cohort,
        "createdAt": now.isoformat(),
        "externalId": external_id,
        "reRegistered": False,
    }


@app.get("/health")
async def health() -> dict:
    info: dict[str, Any] = {
        "ok": True,
        "service": "sair-backend",
        "version": app.version,
        "uptime": monitoring.get_uptime_formatted(),
        "requests_served": monitoring.get_total_requests(),
    }
    try:
        await db.state.client.admin.command("ping")
        info["mongo"] = "ok"
    except Exception as e:
        info["ok"] = False
        info["mongo"] = f"error: {e}"
    return info


@app.get("/mets")
async def root_mets(request: Request):
    """Convenience alias for /admin/mets.
    
    If authenticated as an admin, redirects directly to /admin/mets.
    Otherwise redirects to /admin/login.
    """
    if admin.auth.is_authenticated(request):
        return RedirectResponse(url="/admin/mets", status_code=302)
    return RedirectResponse(url="/admin/login", status_code=302)


@app.post("/score", response_model=ScoreResponse)
async def score(req: ScoreRequest, participant: dict = Depends(current_participant)) -> ScoreResponse:
    if participant.get("blocked"):
        raise HTTPException(
            status_code=403,
            detail="Participant is blocked",
        )

    if len(req.text) < _min_text_length():
        raise HTTPException(
            status_code=400,
            detail=f"text below {_min_text_length()}-char threshold",
        )

    try:
        rate_limit.check_and_record_score(_participant_id(participant))
    except rate_limit.RateLimitExceeded as e:
        raise HTTPException(
            status_code=429,
            detail=str(e),
            headers={"Retry-After": str(e.retry_after)},
        )

    coll = db.state.db.scored_posts

    # 1. Exact (platform, postId) hit.
    existing = await coll.find_one({"platform": req.platform, "postId": req.postId})
    if existing:
        return ScoreResponse(cached=True, source="postId", detector=existing["detector"])

    # 2. textHash hit — same content reposted under a different postId.
    text_hash = _hash_text(req.text)
    by_hash = await coll.find_one({"textHash": text_hash})
    if by_hash:
        doc = {
            "platform": req.platform,
            "postId": req.postId,
            "textHash": text_hash,
            "text": req.text,
            "detector": by_hash["detector"],
            "firstScoredAt": datetime.now(timezone.utc),
            "firstScoredByUserId": _participant_id(participant),
            "scoreSource": "textHash-hit",
            "aliasOf": by_hash["_id"],
        }
        try:
            await coll.insert_one(doc)
        except DuplicateKeyError:
            # Another request inserted in the gap; re-fetch.
            existing = await coll.find_one({"platform": req.platform, "postId": req.postId})
            if existing:
                return ScoreResponse(cached=True, source="postId", detector=existing["detector"])
        return ScoreResponse(cached=True, source="textHash", detector=by_hash["detector"])

    # 3. Full miss → call Pangram.
    try:
        detector = await pangram.score_text(req.text)
    except pangram.PangramError as e:
        raise HTTPException(status_code=502, detail=str(e))

    doc = {
        "platform": req.platform,
        "postId": req.postId,
        "textHash": text_hash,
        "text": req.text,
        "detector": detector,
        "firstScoredAt": datetime.now(timezone.utc),
        "firstScoredByUserId": _participant_id(participant),
        "scoreSource": "pangram",
    }
    try:
        await coll.insert_one(doc)
    except DuplicateKeyError:
        existing = await coll.find_one({"platform": req.platform, "postId": req.postId})
        if existing:
            return ScoreResponse(cached=True, source="postId", detector=existing["detector"])

    return ScoreResponse(cached=False, source="pangram", detector=detector)


@app.post("/observations")
async def add_observations(
    records: list[ObservationIn] | ObservationIn,
    participant: dict = Depends(current_participant),
) -> dict:
    if participant.get("blocked"):
        raise HTTPException(status_code=403, detail="Participant is blocked")
    items = records if isinstance(records, list) else [records]
    if not items:
        return {"inserted": 0}

    user_id = _participant_id(participant)

    try:
        rate_limit.check_and_record_observations(user_id, len(items))
    except rate_limit.RateLimitExceeded as e:
        raise HTTPException(
            status_code=429,
            detail=str(e),
            headers={"Retry-After": str(e.retry_after)},
        )

    now = datetime.now(timezone.utc)
    inserted = 0
    for r in items:
        d = r.model_dump()
        d["userId"] = user_id
        d["recordedAt"] = now
        # Upsert observation doc so full metadata is set, while preserving any
        # existing dwellMs / dwellCount that may have accumulated.
        update_doc = {
            "$set": {
                k: v for k, v in d.items() if k not in ("dwellMs", "dwellCount")
            },
            "$setOnInsert": {
                "dwellMs": 0,
                "dwellCount": 0,
            },
        }
        res = await db.state.db.observations.update_one(
            {"userId": user_id, "platform": d["platform"], "postId": d["postId"]},
            update_doc,
            upsert=True,
        )
        if res.upserted_id is not None:
            inserted += 1
    return {"inserted": inserted, "userId": user_id, "received": len(items)}


@app.post("/dwell")
async def record_dwell(
    payload: DwellIn,
    participant: dict = Depends(current_participant),
) -> dict:
    if participant.get("blocked"):
        raise HTTPException(status_code=403, detail="Participant is blocked")
    """Record participant dwell time on posts.

    Logs granular dwell chunks/episodes into `dwell_logs` and atomically
    increments `dwellMs` and `dwellCount` on the matching `observations` record.
    Short or unscored posts (which are not in observations or scored_posts)
    are ignored so they never create empty observation stubs or pollute dwell_logs.
    """
    items = payload.events if isinstance(payload.events, list) else [payload.events]
    if not items:
        return {"inserted": 0}

    user_id = _participant_id(participant)
    now = datetime.now(timezone.utc)
    valid_docs = []

    for it in items:
        # Check if an observation already exists
        has_obs = await db.state.db.observations.find_one(
            {"userId": user_id, "platform": it.platform, "postId": it.postId}
        )
        is_scored = None
        if not has_obs:
            # Check if post is scored (valid post >= 250 chars whose observation is in-flight)
            is_scored = await db.state.db.scored_posts.find_one(
                {"platform": it.platform, "postId": it.postId}
            )

        # If post is neither observed nor scored (e.g. short post < 250 chars), ignore it
        if not has_obs and not is_scored:
            continue

        is_anomaly = it.dwellMs >= 60000
        valid_docs.append({
            "userId": user_id,
            "platform": it.platform,
            "postId": it.postId,
            "dwellMs": it.dwellMs,
            "isAnomaly": is_anomaly,
            "occurredAt": it.occurredAt,
            "recordedAt": now,
        })

        session_entry = {
            "ms": it.dwellMs,
            "at": now,
            "anomaly": is_anomaly,
        }

        set_fields = {"lastDwellAt": now}
        if is_anomaly:
            set_fields["hasAnomaly"] = True

        update_ops: dict[str, Any] = {
            "$inc": {"dwellMs": it.dwellMs, "dwellCount": 1},
            "$set": set_fields,
            "$push": {
                "dwellSessions": {
                    "$each": [session_entry],
                    "$slice": -10,
                }
            },
        }

        if has_obs:
            await db.state.db.observations.update_one(
                {"_id": has_obs["_id"]},
                update_ops,
            )
        else:
            update_ops["$setOnInsert"] = {
                "userId": user_id,
                "platform": it.platform,
                "postId": it.postId,
                "recordedAt": now,
                "seenAt": it.occurredAt,
            }
            await db.state.db.observations.update_one(
                {"userId": user_id, "platform": it.platform, "postId": it.postId},
                update_ops,
                upsert=True,
            )

    if valid_docs:
        await db.state.db.dwell_logs.insert_many(valid_docs, ordered=False)

    return {"ok": True, "count": len(valid_docs)}



@app.post("/interactions")
async def add_interaction(
    record: InteractionIn,
    participant: dict = Depends(current_participant),
) -> dict:
    if participant.get("blocked"):
        raise HTTPException(status_code=403, detail="Participant is blocked")
    """Append-only event log of participant click intents on Like / Comment /
    Repost / Send buttons. No rate limiting yet — interaction volume is much
    lower than observation volume in practice. One row per click event; we
    deliberately don't dedupe so repeated toggles (like → unlike → like) all
    show up as distinct events.
    """
    user_id = _participant_id(participant)
    doc = {
        "userId": user_id,
        "platform": record.platform,
        "postId": record.postId,
        "action": record.action,
        "occurredAt": record.occurredAt,
        "recordedAt": datetime.now(timezone.utc),
    }
    result = await db.state.db.interactions.insert_one(doc)
    return {"inserted": True, "id": str(result.inserted_id)}


@app.get("/observations")
async def get_observations(
    user: str | None = Query(None, description="participantId to fetch (admin only)"),
    limit: int = Query(5000, ge=1, le=50000),
    join_detector: bool = Query(True),
    participant: dict = Depends(current_participant),
) -> dict:
    caller_id = _participant_id(participant)
    is_admin = participant.get("role") == "admin"
    target = user if (user and is_admin) else caller_id
    if user and not is_admin and user != caller_id:
        raise HTTPException(status_code=403, detail="not authorized to view other participants")

    cursor = (
        db.state.db.observations
        .find({"userId": target})
        .sort("recordedAt", -1)
        .limit(limit)
    )
    observations = [_stringify_ids(d) async for d in cursor]

    if join_detector and observations:
        keys = list({(o["platform"], o["postId"]) for o in observations})
        or_clause = [{"platform": p, "postId": pid} for p, pid in keys]
        scored_cursor = db.state.db.scored_posts.find(
            {"$or": or_clause},
            projection={"platform": 1, "postId": 1, "detector": 1, "text": 1, "scoreSource": 1},
        )
        scored_index: dict[tuple[str, str], dict] = {}
        async for s in scored_cursor:
            scored_index[(s["platform"], s["postId"])] = s
        for o in observations:
            match = scored_index.get((o["platform"], o["postId"]))
            if match:
                o["text"] = match.get("text")
                o["detector"] = match.get("detector")
                o["scoreSource"] = match.get("scoreSource")
            else:
                o["text"] = None
                o["detector"] = None

    return {"userId": target, "count": len(observations), "observations": observations}
