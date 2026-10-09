"""All admin routes — pages + JSON APIs + CSV/JSON exports.

Mounted in main.py with `app.include_router(admin.router)`. Self-contained:
the only main.py change is the include.
"""

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bson import ObjectId
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

import db

from . import auth, queries


_HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))

router = APIRouter(prefix="/admin", tags=["admin"])
# Static files are mounted on the FastAPI app in admin.setup(), not here —
# APIRouter.mount() doesn't resolve paths the same way and silently 404s.


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
    counts = await queries.overview_counts(d)
    dist = await queries.score_distribution(d)
    per_day = await queries.observations_per_day(d, days=30)
    return templates.TemplateResponse(request, "overview.html", {
        "active": "overview",
        "counts": counts,
        "distribution": dist,
        "per_day": per_day,
    })


@router.get("/participants", response_class=HTMLResponse)
async def participants_page(
    request: Request,
    group: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    if (r := _guard(request)) is not None:
        return r
    d = db.state.db
    skip = (page - 1) * page_size
    rows = await queries.list_participants(d, group=group, skip=skip, limit=page_size)
    
    query = {}
    if group:
        query["group"] = group
    total = await d.participants.count_documents(query)
    
    return templates.TemplateResponse(request, "participants.html", {
        "active": "participants",
        "rows": rows,
        "filters": {"group": group},
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size or 1,
    })


@router.get("/participants/{participant_id}", response_class=HTMLResponse)
async def participant_detail_page(request: Request, participant_id: str):
    if (r := _guard(request)) is not None:
        return r
    d = db.state.db
    p = await queries.get_participant(d, participant_id)
    if not p:
        raise HTTPException(status_code=404, detail="Participant not found")
    obs, _ = await queries.list_observations(
        d, user_id=participant_id, skip=0, limit=50, join_detector=True
    )
    inters, _ = await queries.list_interactions(
        d, user_id=participant_id, skip=0, limit=50
    )
    # Per-tier breakdown for this participant only
    tier_counts = {"human": 0, "mixed": 0, "ai": 0, "unscored": 0}
    full_obs, _ = await queries.list_observations(
        d, user_id=participant_id, skip=0, limit=10000, join_detector=True
    )
    for o in full_obs:
        tier_counts[o.get("tier", "unscored")] += 1
    return templates.TemplateResponse(request, "participant_detail.html", {
        "active": "participants",
        "p": p,
        "observations": obs,
        "interactions": inters,
        "tier_counts": tier_counts,
    })


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
    skip = (page - 1) * page_size
    docs, total = await queries.list_observations(
        d, user_id=user_id, platform=platform,
        since=_parse_date(since), until=_parse_date(until),
        skip=skip, limit=page_size, join_detector=True,
    )
    return templates.TemplateResponse(request, "observations.html", {
        "active": "observations",
        "docs": docs,
        "filters": {"user_id": user_id, "platform": platform, "since": since, "until": until},
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size or 1,
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
    skip = (page - 1) * page_size
    docs, total = await queries.list_interactions(
        d, user_id=user_id, platform=platform, action=action,
        skip=skip, limit=page_size,
    )
    return templates.TemplateResponse(request, "interactions.html", {
        "active": "interactions",
        "docs": docs,
        "filters": {"user_id": user_id, "platform": platform, "action": action},
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size or 1,
    })


# ─────────────────────────── Exports ───────────────────────────

async def _stream_csv_observations(d) -> Any:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "recordedAt", "userId", "platform", "postId", "author",
        "authorDisplayName", "authorHeadline", "authorConnectionDegree",
        "authorPremium", "authorVerified", "authorVerifiedType",
        "postedAt", "postedAtRelative", "url", "seenAt",
        "likes", "comments", "reposts", "retweets", "replies", "views",
        "tier", "fraction_ai", "prediction_short", "text",
    ])
    yield buf.getvalue(); buf.seek(0); buf.truncate(0)

    cursor = d.observations.find().sort("recordedAt", -1)
    # We don't pre-join — too many rows. Look up each scored_post once.
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


@router.get("/export/full.json", dependencies=[Depends(auth.require_admin)])
async def export_full_json():
    d = db.state.db
    payload = {
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "participants": [p async for p in d.participants.find({}, projection={"token": 0})],
        "observations": [o async for o in d.observations.find()],
        "interactions": [i async for i in d.interactions.find()],
        "scored_posts": [s async for s in d.scored_posts.find()],
    }
    body = json.dumps(payload, default=_json_default, indent=2)
    return StreamingResponse(
        iter([body]),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="sair_full_export.json"'},
    )


# ─────────────────────────── JSON APIs (handy for ad-hoc curl) ───────────────────────────

@router.get("/api/overview", dependencies=[Depends(auth.require_admin)])
async def api_overview():
    d = db.state.db
    return JSONResponse({
        "counts": await queries.overview_counts(d),
        "distribution": await queries.score_distribution(d),
        "per_day": await queries.observations_per_day(d, days=30),
    })
