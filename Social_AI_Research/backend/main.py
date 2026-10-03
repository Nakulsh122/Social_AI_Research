import hashlib
import os
import random
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from pymongo.errors import BulkWriteError, DuplicateKeyError

import db
import pangram
import rate_limit
from auth import current_participant
import admin


# ─────────────────────────── App lifecycle ───────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    try:
        yield
    finally:
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

# Researcher dashboard — all routes live under /admin/* and are self-contained
# in the admin/ package. Auth is via a single shared password (ADMIN_SECRET).
admin.setup(app)


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


class InteractionIn(BaseModel):
    platform: str = Field(..., pattern="^(twitter|linkedin)$")
    postId: str = Field(..., min_length=1)
    # Free string so we can add new action types without schema changes; the
    # client side controls the vocabulary (currently like_click, comment_click,
    # repost_click, send_click).
    action: str = Field(..., min_length=1, max_length=64)
    occurredAt: int | None = None


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


@app.post("/register")
async def register(request: Request) -> dict:
    """Mint a fresh participant identity. Called by the extension on first
    launch — no auth required. The extension stores the returned token and
    sends it as `Authorization: Bearer <token>` on every subsequent request.

    Optional body `{externalId: "..."}` — typically a Prolific participant ID
    or other recruitment-platform identifier. Stored as-is on the participant
    document; no format validation here.

    Each call creates a new participant row. Uninstall/reinstall = new ID;
    cross-device users get one ID per install. This is intentional given the
    no-friction install model.
    """
    try:
        rate_limit.check_and_record_register(_client_ip(request))
    except rate_limit.RateLimitExceeded as e:
        raise HTTPException(
            status_code=429,
            detail=str(e),
            headers={"Retry-After": str(e.retry_after)},
        )

    external_id: str | None = None
    try:
        raw = await request.body()
        if raw:
            payload = RegisterRequest.model_validate_json(raw)
            external_id = (payload.externalId or "").strip() or None
    except Exception:
        external_id = None

    participant_id = f"u-{uuid.uuid4().hex[:16]}"
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)

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
        "active": True,
        "createdAt": now,
        "source": "self-register",
    }
    if external_id:
        doc["externalId"] = external_id
    await db.state.db.participants.insert_one(doc)
    return {
        "participantId": participant_id,
        "token": token,
        "group": group,
        "createdAt": now.isoformat(),
        "externalId": external_id,
    }


@app.get("/health")
async def health() -> dict:
    info: dict[str, Any] = {"ok": True, "service": "sair-backend", "version": app.version}
    try:
        await db.state.client.admin.command("ping")
        info["mongo"] = "ok"
    except Exception as e:
        info["ok"] = False
        info["mongo"] = f"error: {e}"
    return info


@app.post("/score", response_model=ScoreResponse)
async def score(req: ScoreRequest, participant: dict = Depends(current_participant)) -> ScoreResponse:
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
    docs = []
    for r in items:
        d = r.model_dump()
        d["userId"] = user_id
        d["recordedAt"] = now
        docs.append(d)

    # ordered=False so a duplicate on one doc doesn't abort the rest of the
    # batch. The unique index on (userId, platform, postId) silently rejects
    # repeats from tab reloads / page navigations — exposure for the study is
    # counted once per (user, post), not once per render.
    try:
        result = await db.state.db.observations.insert_many(docs, ordered=False)
        inserted = len(result.inserted_ids)
    except BulkWriteError as bwe:
        inserted = bwe.details.get("nInserted", 0)
        non_dup = [e for e in bwe.details.get("writeErrors", []) if e.get("code") != 11000]
        if non_dup:
            raise
    return {"inserted": inserted, "userId": user_id, "received": len(docs)}


@app.post("/interactions")
async def add_interaction(
    record: InteractionIn,
    participant: dict = Depends(current_participant),
) -> dict:
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
