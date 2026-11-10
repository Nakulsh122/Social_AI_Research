"""All admin routes — pages + JSON APIs + CSV/JSON/ZIP exports.

Mounted in main.py with `app.include_router(admin.router)`. Self-contained.
"""

import csv
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bson import ObjectId
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

import db
import monitoring
import time

from . import auth, queries


_HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))

router = APIRouter(prefix="/admin", tags=["admin"])


# ─────────────────────────── Login / logout ───────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if auth.is_authenticated(request):
        return RedirectResponse(url="/admin/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, password: str = Form(...)):
    if not auth.verify_password(password):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Incorrect password"},
            status_code=401,
        )
    resp = RedirectResponse(url="/admin/", status_code=302)
    resp.set_cookie(
        key=auth.COOKIE_NAME,
        value=auth.expected_cookie_value(),
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=60 * 60 * 8,  # 8 hours
        path="/admin",
    )
    return resp


@router.post("/logout")
async def logout():
    resp = RedirectResponse(url="/admin/login", status_code=302)
    resp.delete_cookie(auth.COOKIE_NAME, path="/admin")
    return resp


# ─────────────────────────── Helpers ───────────────────────────

def _guard(request: Request):
    redirect = auth.require_admin_or_redirect(request)
    return redirect  # None if authed, RedirectResponse otherwise


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _json_default(o: Any):
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, ObjectId):
        return str(o)
    return str(o)


# ─────────────────────────── Pages ───────────────────────────

@router.get("/", response_class=HTMLResponse)
async def overview_page(request: Request):
    if (r := _guard(request)) is not None:
        return r
    d = db.state.db
    mongo_down = not await db.is_connected()
    if mongo_down:
        counts = {
            "participants": 0, "observations": 0, "interactions": 0,
            "scored_posts": 0, "dwell_logs": 0,
            "total_dwell_formatted": "N/A", "avg_dwell_formatted": "N/A"
        }
        dist = {"human": 0, "mixed": 0, "ai": 0, "unscored": 0}
        per_day = []
        recent_events = []
    else:
        try:
            counts = await queries.overview_counts(d)
            dist = await queries.score_distribution(d)
            per_day = await queries.observations_per_day(d, days=30)
            recent_events = await queries.recent_active_events(d, limit=20)
        except Exception:
            mongo_down = True
            counts = {
                "participants": 0, "observations": 0, "interactions": 0,
                "scored_posts": 0, "dwell_logs": 0,
                "total_dwell_formatted": "N/A", "avg_dwell_formatted": "N/A"
            }
            dist = {"human": 0, "mixed": 0, "ai": 0, "unscored": 0}
            per_day = []
            recent_events = []

    return templates.TemplateResponse(request, "overview.html", {
        "active": "overview",
        "counts": counts,
        "distribution": dist,
        "per_day": per_day,
        "recent_events": recent_events,
        "mongo_down": mongo_down,
    })


REQUIRED_SECRET = "COD_DOC"


@router.get("/participants", response_class=HTMLResponse)
async def participants_page(
    request: Request,
    group: str | None = Query(None),
    cohort: int | None = Query(None),
    status: str | None = Query(None),
    q: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    if (r := _guard(request)) is not None:
        return r
    d = db.state.db
    mongo_down = not await db.is_connected()
    if mongo_down:
        rows = []
        distinct_cohorts = [1]
        blocked_count = 0
        total = 0
        total_pages = 1
    else:
        try:
            skip = (page - 1) * page_size
            search_str = q.strip() if q and q.strip() else None
            rows = await queries.list_participants(d, group=group, cohort=cohort, status=status, search=search_str, skip=skip, limit=page_size)
            distinct_cohorts = await queries.get_distinct_cohorts(d)

            query: dict[str, Any] = {}
            if group:
                query["group"] = group
            if status == "blocked":
                query["blocked"] = True
            elif status == "active":
                query["$or"] = [{"blocked": False}, {"blocked": {"$exists": False}}]

            clauses: list[dict[str, Any]] = []
            if cohort is not None:
                if cohort == 1:
                    clauses.append({"$or": [{"cohort": 1}, {"cohort": {"$exists": False}}, {"round": 1}]})
                else:
                    clauses.append({"$or": [{"cohort": cohort}, {"round": cohort}]})

            if search_str:
                import re
                regex = {"$regex": re.escape(search_str), "$options": "i"}
                clauses.append({"$or": [
                    {"externalId": regex},
                    {"participantId": regex},
                    {"timezone": regex},
                ]})

            if len(clauses) == 1:
                query.update(clauses[0])
            elif len(clauses) > 1:
                query["$and"] = clauses

            total = await d.participants.count_documents(query)
            blocked_count = await d.participants.count_documents({"blocked": True})
            total_pages = (total + page_size - 1) // page_size or 1
        except Exception:
            mongo_down = True
            rows = []
            distinct_cohorts = [1]
            blocked_count = 0
            total = 0
            total_pages = 1
    
    return templates.TemplateResponse(request, "participants.html", {
        "active": "participants",
        "rows": rows,
        "filters": {"group": group, "cohort": cohort, "status": status, "q": q or ""},
        "distinct_cohorts": distinct_cohorts,
        "blocked_count": blocked_count,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "mongo_down": mongo_down,
    })


@router.get("/participants/{participant_id}", response_class=HTMLResponse)
async def participant_detail_page(request: Request, participant_id: str):
    if (r := _guard(request)) is not None:
        return r
    d = db.state.db
    mongo_down = not await db.is_connected()
    if mongo_down:
        p = {"participantId": participant_id, "externalId": "N/A", "role": "participant", "group": "N/A", "active": False}
        obs = []
        inters = []
        tier_counts = {"human": 0, "mixed": 0, "ai": 0, "unscored": 0}
    else:
        try:
            p = await queries.get_participant(d, participant_id)
            if not p:
                raise HTTPException(status_code=404, detail="Participant not found")
            obs, _ = await queries.list_observations(
                d, user_id=participant_id, skip=0, limit=50, join_detector=True
            )
            inters, _ = await queries.list_interactions(
                d, user_id=participant_id, skip=0, limit=50
            )
            tier_counts = {"human": 0, "mixed": 0, "ai": 0, "unscored": 0}
            full_obs, _ = await queries.list_observations(
                d, user_id=participant_id, skip=0, limit=10000, join_detector=True
            )
            for o in full_obs:
                tier_counts[o.get("tier", "unscored")] += 1
        except HTTPException:
            raise
        except Exception:
            mongo_down = True
            p = {"participantId": participant_id, "externalId": "N/A", "role": "participant", "group": "N/A", "active": False}
            obs = []
            inters = []
            tier_counts = {"human": 0, "mixed": 0, "ai": 0, "unscored": 0}

    return templates.TemplateResponse(request, "participant_detail.html", {
        "active": "participants",
        "p": p,
        "observations": obs,
        "interactions": inters,
        "tier_counts": tier_counts,
        "mongo_down": mongo_down,
    })


@router.post("/participants/{participant_id}/edit")
async def edit_participant_endpoint(
    request: Request,
    participant_id: str,
):
    if (r := _guard(request)) is not None:
        return r
    d = db.state.db

    content_type = request.headers.get("content-type", "")
    secret = None
    updates: dict[str, Any] = {}

    if "application/json" in content_type:
        try:
            body = await request.json()
            secret = str(body.get("secret", "")).strip()
            if "group" in body: updates["group"] = str(body["group"]).strip()
            if "role" in body: updates["role"] = str(body["role"]).strip()
            if "cohort" in body and body["cohort"] is not None and str(body["cohort"]).strip():
                try: updates["cohort"] = int(body["cohort"])
                except (ValueError, TypeError): pass
            if "externalId" in body: updates["externalId"] = str(body["externalId"]).strip() or None
            if "timezone" in body: updates["timezone"] = str(body["timezone"]).strip()
            if "active" in body: updates["active"] = bool(body["active"])
            if "blocked" in body: updates["blocked"] = bool(body["blocked"])
        except Exception:
            raise HTTPException(status_code=400, detail="Malformed JSON body")
    else:
        form = await request.form()
        secret = str(form.get("secret", "")).strip()
        if "group" in form: updates["group"] = str(form.get("group", "")).strip()
        if "role" in form: updates["role"] = str(form.get("role", "")).strip()
        if "cohort" in form and str(form.get("cohort", "")).strip():
            try: updates["cohort"] = int(form.get("cohort"))
            except (ValueError, TypeError): pass
        if "externalId" in form: updates["externalId"] = str(form.get("externalId", "")).strip() or None
        if "timezone" in form: updates["timezone"] = str(form.get("timezone", "")).strip()
        if "active" in form: updates["active"] = str(form.get("active", "")).lower() in ("true", "1", "yes", "on")
        if "blocked" in form: updates["blocked"] = str(form.get("blocked", "")).lower() in ("true", "1", "yes", "on")

    if secret != REQUIRED_SECRET:
        if "application/json" in content_type or request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse(
                status_code=403,
                content={"ok": False, "error": f"Invalid confirmation secret. You must enter '{REQUIRED_SECRET}' to modify a participant."}
            )
        raise HTTPException(
            status_code=403,
            detail=f"Invalid confirmation secret. You must enter '{REQUIRED_SECRET}' to modify a participant."
        )

    # Sync with blocked_participants if blocked status was modified
    if "blocked" in updates:
        if updates["blocked"]:
            updates["blockedAt"] = datetime.now(timezone.utc)
            updates["blockedReason"] = "Modified in Admin UI"
            # Add to blocked_participants
            part_doc = await d.participants.find_one({"participantId": participant_id})
            if part_doc:
                for ident in [part_doc.get("externalId"), participant_id, str(part_doc.get("_id", ""))]:
                    if ident:
                        await d.blocked_participants.update_one(
                            {"identifier": ident},
                            {"$set": {
                                "identifier": ident,
                                "identifierType": "prolific_id" if ident == part_doc.get("externalId") else "participant_id",
                                "participantId": participant_id,
                                "externalId": part_doc.get("externalId"),
                                "reason": "Admin modification",
                                "blockedAt": updates["blockedAt"],
                                "active": True
                            }},
                            upsert=True
                        )
        else:
            # Unblocked: remove from blocked_participants
            await queries.unblock_identifier(d, participant_id)

    success = await queries.update_participant(d, participant_id, updates)
    if not success:
        raise HTTPException(status_code=404, detail="Participant not found or nothing to update")

    if "application/json" in content_type or request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse({"ok": True, "message": f"Participant {participant_id} updated successfully"})

    return RedirectResponse(url=f"/admin/participants/{participant_id}?updated=1", status_code=303)


@router.post("/participants/{participant_id}/delete")
async def delete_participant_endpoint(
    request: Request,
    participant_id: str,
):
    if (r := _guard(request)) is not None:
        return r
    d = db.state.db

    content_type = request.headers.get("content-type", "")
    secret = None
    cascade = True

    if "application/json" in content_type:
        try:
            body = await request.json()
            secret = str(body.get("secret", "")).strip()
            cascade = bool(body.get("cascade", True))
        except Exception:
            raise HTTPException(status_code=400, detail="Malformed JSON body")
    else:
        form = await request.form()
        secret = str(form.get("secret", "")).strip()
        cascade = str(form.get("cascade", "true")).lower() in ("true", "1", "yes", "on")

    if secret != REQUIRED_SECRET:
        if "application/json" in content_type or request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse(
                status_code=403,
                content={"ok": False, "error": f"Invalid confirmation secret. You must enter '{REQUIRED_SECRET}' to delete a participant."}
            )
        raise HTTPException(
            status_code=403,
            detail=f"Invalid confirmation secret. You must enter '{REQUIRED_SECRET}' to delete a participant."
        )

    res = await queries.delete_participant(d, participant_id, cascade=cascade)
    if not res.get("participantDeleted"):
        raise HTTPException(status_code=404, detail="Participant not found")

    if "application/json" in content_type or request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse({"ok": True, "message": f"Participant {participant_id} deleted successfully", "details": res})

    return RedirectResponse(url="/admin/participants?deleted=1", status_code=303)


@router.get("/observations", response_class=HTMLResponse)
async def observations_page(
    request: Request,
    user_id: str | None = Query(None),
    platform: str | None = Query(None),
    since: str | None = Query(None),
    until: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    if (r := _guard(request)) is not None:
        return r
    d = db.state.db
    mongo_down = not await db.is_connected()
    if mongo_down:
        docs = []
        total = 0
    else:
        try:
            skip = (page - 1) * page_size
            docs, total = await queries.list_observations(
                d, user_id=user_id, platform=platform,
                since=_parse_date(since), until=_parse_date(until),
                skip=skip, limit=page_size, join_detector=True,
            )
        except Exception:
            mongo_down = True
            docs = []
            total = 0
    return templates.TemplateResponse(request, "observations.html", {
        "active": "observations",
        "docs": docs,
        "filters": {"user_id": user_id, "platform": platform, "since": since, "until": until},
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size or 1,
        "mongo_down": mongo_down,
    })


@router.get("/interactions", response_class=HTMLResponse)
async def interactions_page(
    request: Request,
    user_id: str | None = Query(None),
    platform: str | None = Query(None),
    action: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    if (r := _guard(request)) is not None:
        return r
    d = db.state.db
    mongo_down = not await db.is_connected()
    if mongo_down:
        docs = []
        total = 0
    else:
        try:
            skip = (page - 1) * page_size
            docs, total = await queries.list_interactions(
                d, user_id=user_id, platform=platform, action=action,
                skip=skip, limit=page_size,
            )
        except Exception:
            mongo_down = True
            docs = []
            total = 0
    return templates.TemplateResponse(request, "interactions.html", {
        "active": "interactions",
        "docs": docs,
        "filters": {"user_id": user_id, "platform": platform, "action": action},
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size or 1,
        "mongo_down": mongo_down,
    })


# ─────────────────────────── Monitoring / System Metrics ───────────────────────────

@router.get("/metrics", response_class=HTMLResponse)
@router.get("/mets", response_class=HTMLResponse)
async def metrics_page(request: Request, format: str | None = Query(None)):
    """Interactive admin monitoring dashboard and metrics endpoint.
    
    Protected by admin authentication. Tracks in-memory request volume, latencies,
    and system resource utilisation without writing any data to the database.
    """
    if (r := _guard(request)) is not None:
        return r

    db_health = {"connected": False, "status": "disconnected", "ping_ms": None, "storage_mode": "in-memory-only (0 db writes)"}
    if hasattr(db, "state") and db.state.client is not None:
        try:
            t0 = time.perf_counter()
            await db.state.client.admin.command("ping")
            db_health = {
                "connected": True,
                "status": "ok",
                "ping_ms": round((time.perf_counter() - t0) * 1000, 2),
                "storage_mode": "in-memory-only (0 db writes)",
            }
        except Exception as e:
            db_health = {
                "connected": False,
                "status": f"error: {e}",
                "ping_ms": None,
                "storage_mode": "in-memory-only (0 db writes)",
            }

    snap = monitoring.get_metrics_snapshot(db_health=db_health)

    # Return pure JSON when requested via query parameter or Accept header
    if format == "json" or request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse(snap)

    return templates.TemplateResponse(request, "metrics.html", {
        "active": "metrics",
        "metrics": snap,
    })


@router.get("/api/metrics")
@router.get("/api/mets")
async def metrics_api(request: Request):
    """Direct JSON API endpoint for system metrics polling by admin dashboard or scripts."""
    if not auth.is_authenticated(request):
        raise HTTPException(status_code=401, detail="Admin authentication required")

    db_health = {"connected": False, "status": "disconnected", "ping_ms": None, "storage_mode": "in-memory-only (0 db writes)"}
    if hasattr(db, "state") and db.state.client is not None:
        try:
            t0 = time.perf_counter()
            await db.state.client.admin.command("ping")
            db_health = {
                "connected": True,
                "status": "ok",
                "ping_ms": round((time.perf_counter() - t0) * 1000, 2),
                "storage_mode": "in-memory-only (0 db writes)",
            }
        except Exception as e:
            db_health = {
                "connected": False,
                "status": f"error: {e}",
                "ping_ms": None,
                "storage_mode": "in-memory-only (0 db writes)",
            }

    return JSONResponse(monitoring.get_metrics_snapshot(db_health=db_health))


# ─────────────────────────── Exports ───────────────────────────

async def _stream_csv_participants(d) -> Any:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "participantId", "externalId", "group", "cohort", "role", "timezone",
        "createdAt", "enrolledAt", "lastSeen", "observationCount", "interactionCount",
        "totalDwellMs", "totalDwellFormatted",
        "first7ActiveDays", "first7Status", "totalActiveDaysAllTime", "totalStudyDays",
    ])
    yield buf.getvalue(); buf.seek(0); buf.truncate(0)

    cursor = d.participants.find().sort("createdAt", -1)
    async for p in cursor:
        pid = p.get("participantId") or str(p["_id"])
        obs_count = await d.observations.count_documents({"userId": pid})
        int_count = await d.interactions.count_documents({"userId": pid})
        last_obs = await d.observations.find_one(
            {"userId": pid}, sort=[("recordedAt", -1)], projection={"recordedAt": 1}
        )
        dwell_res = await d.observations.aggregate([
            {"$match": {"userId": pid}},
            {"$group": {"_id": None, "t": {"$sum": "$dwellMs"}}}
        ]).to_list(1)
        tot_dwell = dwell_res[0]["t"] if dwell_res else 0
        comp = await queries.participant_compliance_stats(d, p)

        c_val = p.get("cohort", p.get("round", 1))
        enrolled_dt = p.get("enrolledAt") or p.get("createdAt")
        w.writerow([
            pid,
            p.get("externalId", ""),
            p.get("group", ""),
            int(c_val) if c_val is not None else 1,
            p.get("role", "participant"),
            p.get("timezone", "America/New_York"),
            p.get("createdAt").isoformat() if p.get("createdAt") else "",
            enrolled_dt.isoformat() if enrolled_dt else "",
            last_obs.get("recordedAt").isoformat() if (last_obs and last_obs.get("recordedAt")) else "",
            obs_count,
            int_count,
            tot_dwell,
            queries.format_dwell_ms(tot_dwell),
            comp["complianceRatio"],
            comp["statusLabel"],
            comp["totalQualifyingDays"],
            comp["totalStudyDays"],
        ])
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)


async def _stream_csv_daily_compliance(d) -> Any:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "participantId", "externalId", "group", "cohort", "timezone",
        "date", "studyDay", "isFirst7Days",
        "activeMinutes", "dwellSeconds", "interactions", "qualifies10Min",
    ])
    yield buf.getvalue(); buf.seek(0); buf.truncate(0)

    cursor = d.participants.find().sort("createdAt", -1)
    async for p in cursor:
        pid = p.get("participantId") or str(p["_id"])
        comp = await queries.participant_compliance_stats(d, p)
        c_val = p.get("cohort", p.get("round", 1))
        for m in comp["matrix"]:
            if m["isFuture"]:
                continue
            w.writerow([
                pid,
                p.get("externalId", ""),
                p.get("group", ""),
                int(c_val) if c_val is not None else 1,
                comp["timezone"],
                m["date"],
                m["day"],
                "YES" if m["isFirst7Days"] else "NO",
                m["activeMinutes"],
                round(m["dwellMs"] / 1000.0, 1),
                m["interactions"],
                "YES" if m["qualifies"] else "NO",
            ])
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)


async def _stream_csv_observations(d) -> Any:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "recordedAt", "userId", "platform", "postId", "author",
        "authorDisplayName", "authorHeadline", "authorConnectionDegree",
        "authorPremium", "authorVerified", "authorVerifiedType",
        "postedAt", "postedAtRelative", "url", "seenAt",
        "dwellMs", "dwellSeconds",
        "likes", "comments", "reposts", "retweets", "replies", "views",
        "tier", "fraction_ai", "prediction_short", "text",
    ])
    yield buf.getvalue(); buf.seek(0); buf.truncate(0)

    cursor = d.observations.find().sort("recordedAt", -1)
    cache: dict[tuple[str, str], dict] = {}
    async for o in cursor:
        key = (o.get("platform"), o.get("postId"))
        if key not in cache:
            sp = await d.scored_posts.find_one(
                {"platform": key[0], "postId": key[1]},
                projection={"detector": 1, "text": 1},
            )
            cache[key] = sp or {}
        sp = cache[key]
        detector = sp.get("detector") or {}
        eng = o.get("engagement") or {}
        dwell_ms = o.get("dwellMs", 0)
        dwell_sec = round(dwell_ms / 1000.0, 2) if dwell_ms else 0.0
        w.writerow([
            (o.get("recordedAt").isoformat() if o.get("recordedAt") else ""),
            o.get("userId", ""),
            o.get("platform", ""),
            o.get("postId", ""),
            o.get("author", ""),
            o.get("authorDisplayName", ""),
            o.get("authorHeadline", ""),
            o.get("authorConnectionDegree", ""),
            o.get("authorPremium", ""),
            o.get("authorVerified", ""),
            o.get("authorVerifiedType", ""),
            o.get("postedAt", ""),
            o.get("postedAtRelative", ""),
            o.get("url", ""),
            o.get("seenAt", ""),
            dwell_ms,
            dwell_sec,
            eng.get("likes", ""),
            eng.get("comments", ""),
            eng.get("reposts", ""),
            eng.get("retweets", ""),
            eng.get("replies", ""),
            eng.get("views", ""),
            queries.tier_from_detector(detector if detector else None),
            detector.get("fraction_ai", "") if detector else "",
            detector.get("prediction_short", "") if detector else "",
            (sp.get("text") or "").replace("\n", " ").replace("\r", " "),
        ])
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)


async def _stream_csv_interactions(d) -> Any:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["recordedAt", "occurredAt", "userId", "platform", "postId", "action"])
    yield buf.getvalue(); buf.seek(0); buf.truncate(0)
    async for r in d.interactions.find().sort("recordedAt", -1):
        w.writerow([
            (r.get("recordedAt").isoformat() if r.get("recordedAt") else ""),
            r.get("occurredAt", ""),
            r.get("userId", ""),
            r.get("platform", ""),
            r.get("postId", ""),
            r.get("action", ""),
        ])
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)


async def _stream_csv_dwell(d) -> Any:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["recordedAt", "occurredAt", "userId", "platform", "postId", "dwellMs", "dwellSeconds"])
    yield buf.getvalue(); buf.seek(0); buf.truncate(0)
    async for r in d.dwell_logs.find().sort("recordedAt", -1):
        ms = r.get("dwellMs", 0)
        sec = round(ms / 1000.0, 2) if ms else 0.0
        w.writerow([
            (r.get("recordedAt").isoformat() if r.get("recordedAt") else ""),
            r.get("occurredAt", ""),
            r.get("userId", ""),
            r.get("platform", ""),
            r.get("postId", ""),
            ms,
            sec,
        ])
        yield buf.getvalue(); buf.seek(0); buf.truncate(0)


@router.get("/export/all.zip", dependencies=[Depends(auth.require_admin)])
async def export_all_zip():
    """One-click export of ALL study data in a single zip archive."""
    d = db.state.db
    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
        # 1. participants.csv
        p_buf = io.StringIO()
        async for chunk in _stream_csv_participants(d):
            p_buf.write(chunk)
        zipf.writestr("participants.csv", p_buf.getvalue())

        # 2. daily_compliance_summary.csv
        c_buf = io.StringIO()
        async for chunk in _stream_csv_daily_compliance(d):
            c_buf.write(chunk)
        zipf.writestr("daily_compliance_summary.csv", c_buf.getvalue())

        # 3. observations.csv
        o_buf = io.StringIO()
        async for chunk in _stream_csv_observations(d):
            o_buf.write(chunk)
        zipf.writestr("observations.csv", o_buf.getvalue())

        # 4. interactions.csv
        i_buf = io.StringIO()
        async for chunk in _stream_csv_interactions(d):
            i_buf.write(chunk)
        zipf.writestr("interactions.csv", i_buf.getvalue())

        # 5. dwell.csv
        dw_buf = io.StringIO()
        async for chunk in _stream_csv_dwell(d):
            dw_buf.write(chunk)
        zipf.writestr("dwell.csv", dw_buf.getvalue())

        # 6. complete_dataset.json
        payload = {
            "exportedAt": datetime.now(timezone.utc).isoformat(),
            "participants": [p async for p in d.participants.find({}, projection={"token": 0})],
            "observations": [o async for o in d.observations.find()],
            "interactions": [i async for i in d.interactions.find()],
            "dwell_logs": [dw async for dw in d.dwell_logs.find()],
            "scored_posts": [s async for s in d.scored_posts.find()],
        }
        zipf.writestr("complete_dataset.json", json.dumps(payload, default=_json_default, indent=2))

    zip_bytes = zip_buffer.getvalue()
    now_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return StreamingResponse(
        iter([zip_bytes]),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="sair_all_data_{now_str}.zip"',
            "Content-Length": str(len(zip_bytes)),
        },
    )


@router.get("/export/daily_compliance.csv", dependencies=[Depends(auth.require_admin)])
async def export_daily_compliance_csv():
    return StreamingResponse(
        _stream_csv_daily_compliance(db.state.db),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="sair_daily_compliance.csv"'},
    )


@router.get("/export/observations.csv", dependencies=[Depends(auth.require_admin)])
async def export_observations_csv():
    return StreamingResponse(
        _stream_csv_observations(db.state.db),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="sair_observations.csv"'},
    )


@router.get("/export/interactions.csv", dependencies=[Depends(auth.require_admin)])
async def export_interactions_csv():
    return StreamingResponse(
        _stream_csv_interactions(db.state.db),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="sair_interactions.csv"'},
    )


@router.get("/export/dwell.csv", dependencies=[Depends(auth.require_admin)])
async def export_dwell_csv():
    return StreamingResponse(
        _stream_csv_dwell(db.state.db),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="sair_dwell.csv"'},
    )


@router.get("/export/full.json", dependencies=[Depends(auth.require_admin)])
async def export_full_json():
    d = db.state.db
    payload = {
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "participants": [p async for p in d.participants.find({}, projection={"token": 0})],
        "observations": [o async for o in d.observations.find()],
        "interactions": [i async for i in d.interactions.find()],
        "dwell_logs": [dw async for dw in d.dwell_logs.find()],
        "scored_posts": [s async for s in d.scored_posts.find()],
    }
    body = json.dumps(payload, default=_json_default, indent=2)
    return StreamingResponse(
        iter([body]),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="sair_full_export.json"'},
    )


# ─────────────────────────── Blocklist Management ───────────────────────────

def _parse_identifiers_from_text(raw_text: str) -> list[str]:
    import re
    tokens = re.split(r"[\r\n,;\t\s]+", raw_text)
    out = []
    for t in tokens:
        cleaned = t.strip().strip('"').strip("'").strip("\ufeff")
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def _parse_identifiers_from_file_bytes(content: bytes) -> list[str]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            text = content.decode("latin-1", errors="replace")

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return []

    first_row = [c.strip().lower() for c in rows[0]]
    target_cols = {
        "participant id", "participant_id", "prolific id", "prolific_id",
        "externalid", "external_id", "id", "mongoid", "mongo_id", "_id", "participantid"
    }
    target_idx = None
    for idx, col in enumerate(first_row):
        if col in target_cols:
            target_idx = idx
            break

    extracted = []
    if target_idx is not None and len(rows) > 1:
        for r in rows[1:]:
            if len(r) > target_idx:
                val = r[target_idx].strip().strip('"').strip("'")
                if val and val not in extracted:
                    extracted.append(val)
    else:
        for r in rows:
            for cell in r:
                for token in _parse_identifiers_from_text(cell):
                    if token and token not in extracted:
                        extracted.append(token)
    return extracted


@router.get("/blocklist", response_class=HTMLResponse)
async def blocklist_page(
    request: Request,
    q: str | None = Query(None),
    msg: str | None = Query(None),
    error: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    if (r := _guard(request)) is not None:
        return r
    d = db.state.db
    mongo_down = not await db.is_connected()
    if mongo_down:
        blocked_rows = []
        counts = {"active_blocks": 0, "candidate_participants": 0}
        total = 0
        total_pages = 1
    else:
        try:
            skip = (page - 1) * page_size
            search_str = q.strip() if q and q.strip() else None

            blocked_rows, total = await queries.list_blocked(d, skip=skip, limit=page_size, search=search_str)
            counts = await queries.count_blocked(d)
            total_pages = (total + page_size - 1) // page_size or 1
        except Exception:
            mongo_down = True
            blocked_rows = []
            counts = {"active_blocks": 0, "candidate_participants": 0}
            total = 0
            total_pages = 1

    return templates.TemplateResponse(request, "blocklist.html", {
        "active": "blocklist",
        "rows": blocked_rows,
        "counts": counts,
        "q": q or "",
        "msg": msg,
        "error": error,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "required_secret": REQUIRED_SECRET,
        "mongo_down": mongo_down,
    })


@router.post("/blocklist/add")
async def add_blocklist_endpoint(
    request: Request,
    identifiers_text: str = Form(""),
    file: UploadFile | None = File(None),
    reason: str = Form(""),
    secret: str = Form(""),
):
    if (r := _guard(request)) is not None:
        return r
    d = db.state.db

    content_type = request.headers.get("content-type", "")
    all_identifiers = []

    if "application/json" in content_type:
        try:
            body = await request.json()
            secret = str(body.get("secret", "")).strip()
            reason = str(body.get("reason", "")).strip()
            raw_ids = body.get("identifiers", [])
            if isinstance(raw_ids, str):
                all_identifiers.extend(_parse_identifiers_from_text(raw_ids))
            elif isinstance(raw_ids, list):
                for item in raw_ids:
                    all_identifiers.extend(_parse_identifiers_from_text(str(item)))
        except Exception:
            raise HTTPException(status_code=400, detail="Malformed JSON body")
    else:
        if identifiers_text:
            all_identifiers.extend(_parse_identifiers_from_text(identifiers_text))
        if file and file.filename:
            file_bytes = await file.read()
            all_identifiers.extend(_parse_identifiers_from_file_bytes(file_bytes))

    if secret != REQUIRED_SECRET:
        if "application/json" in content_type or request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse(
                status_code=403,
                content={"ok": False, "error": f"Invalid confirmation secret. You must enter '{REQUIRED_SECRET}'."}
            )
        import urllib.parse
        err_msg = f"Invalid confirmation secret. You must enter '{REQUIRED_SECRET}'."
        return RedirectResponse(url=f"/admin/blocklist?error={urllib.parse.quote(err_msg)}", status_code=303)

    unique_ids = list(dict.fromkeys(all_identifiers))
    if not unique_ids:
        if "application/json" in content_type:
            return JSONResponse(status_code=400, content={"ok": False, "error": "No valid identifiers provided."})
        import urllib.parse
        return RedirectResponse(url=f"/admin/blocklist?error={urllib.parse.quote('No valid identifiers found.')}", status_code=303)

    summary = await queries.block_identifiers(d, unique_ids, reason=reason)

    if "application/json" in content_type or request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse({"ok": True, "summary": summary})

    import urllib.parse
    msg = f"Successfully blocked {len(unique_ids)} identifiers ({summary['matched_participants_count']} participants marked, {summary['new_blocklist_entries']} persistent entries added)."
    return RedirectResponse(url=f"/admin/blocklist?msg={urllib.parse.quote(msg)}", status_code=303)


@router.post("/blocklist/remove")
async def remove_blocklist_endpoint(
    request: Request,
    identifier: str = Form(""),
    secret: str = Form(""),
):
    if (r := _guard(request)) is not None:
        return r
    d = db.state.db

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await request.json()
            secret = str(body.get("secret", "")).strip()
            identifier = str(body.get("identifier", "")).strip()
        except Exception:
            raise HTTPException(status_code=400, detail="Malformed JSON")

    if secret != REQUIRED_SECRET:
        if "application/json" in content_type or request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse(
                status_code=403,
                content={"ok": False, "error": f"Invalid confirmation secret. You must enter '{REQUIRED_SECRET}'."}
            )
        import urllib.parse
        return RedirectResponse(url=f"/admin/blocklist?error={urllib.parse.quote('Invalid confirmation secret.')}", status_code=303)

    if not identifier:
        raise HTTPException(status_code=400, detail="Identifier is required")

    success = await queries.unblock_identifier(d, identifier)

    if "application/json" in content_type or request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse({"ok": True, "unblocked": success, "identifier": identifier})

    import urllib.parse
    msg = f"Successfully unblocked {identifier}."
    return RedirectResponse(url=f"/admin/blocklist?msg={urllib.parse.quote(msg)}", status_code=303)


@router.get("/export/blocklist.csv")
async def export_blocklist_csv(request: Request):
    if (r := _guard(request)) is not None:
        return r
    d = db.state.db
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["identifier", "identifierType", "participantId", "externalId", "mongoId", "reason", "blockedAt"])

    cursor = d.blocked_participants.find().sort("blockedAt", -1)
    async for doc in cursor:
        b_at = doc.get("blockedAt")
        b_at_str = b_at.isoformat() if hasattr(b_at, "isoformat") else str(b_at or "")
        writer.writerow([
            doc.get("identifier", ""),
            doc.get("identifierType", ""),
            doc.get("participantId", "") or "",
            doc.get("externalId", "") or "",
            doc.get("mongoId", "") or "",
            doc.get("reason", "") or "",
            b_at_str,
        ])

    val = buf.getvalue()
    return StreamingResponse(
        iter([val]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="blocked_participants.csv"'},
    )


# ─────────────────────────── Single Replaced Hourly Export ───────────────────────────

@router.get("/export/latest_zip")
async def export_latest_zip(request: Request):
    """Download the single replaced exports.zip archive.
    
    If it exists on disk, streams it directly. If not, generates it on the fly.
    """
    if (r := _guard(request)) is not None:
        return r
    backend_dir = Path(__file__).resolve().parent.parent
    zip_path = (backend_dir / "exports.zip").resolve()
    if not zip_path.exists():
        zip_path = Path("exports.zip").resolve()

    if not zip_path.exists():
        import hourly_export
        zip_path = (backend_dir / "exports.zip").resolve()
        await hourly_export.export_database(db.state.db, zip_path)

    with open(zip_path, "rb") as f:
        data = f.read()

    return StreamingResponse(
        iter([data]),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="exports.zip"',
            "Content-Length": str(len(data)),
        },
    )


@router.post("/export/run_hourly_now")
async def run_hourly_now_endpoint(request: Request):
    """Trigger the hourly export script immediately to replace exports.zip."""
    if (r := _guard(request)) is not None:
        return r
    import hourly_export
    backend_dir = Path(__file__).resolve().parent.parent
    zip_path = (backend_dir / "exports.zip").resolve()
    result_path = await hourly_export.export_database(db.state.db, zip_path)
    file_size_kb = round(result_path.stat().st_size / 1024, 1)
    return JSONResponse({
        "ok": True,
        "message": f"Successfully generated and replaced exports.zip ({file_size_kb} KB)",
        "path": str(result_path),
        "size_kb": file_size_kb,
    })



# ─────────────────────────── JSON APIs ───────────────────────────

@router.get("/api/overview", dependencies=[Depends(auth.require_admin)])
async def api_overview():
    d = db.state.db
    return JSONResponse({
        "counts": await queries.overview_counts(d),
        "distribution": await queries.score_distribution(d),
        "per_day": await queries.observations_per_day(d, days=30),
        "recent_events": await queries.recent_active_events(d, limit=20),
    })
