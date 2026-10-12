"""Mongo query helpers for the admin dashboard.

All functions take the `db` (motor database) as their first arg so they're
trivially testable and don't reach into the global state from the regular
backend module.
"""

from datetime import datetime, timedelta, timezone
from typing import Any


# ─────────────────────────── Tier classification ───────────────────────────
# Mirrors the in-extension tierFromDetector logic so the dashboard's score
# distribution matches what participants see.

def tier_from_detector(detector: dict | None) -> str:
    if not detector:
        return "unscored"
    short = (detector.get("prediction_short") or "").lower()
    if short:
        if short in ("ai",):
            return "ai"
        if short in ("mixed", "ai-assisted"):
            return "mixed"
        if short in ("human",):
            return "human"
    frac = detector.get("fraction_ai")
    if isinstance(frac, (int, float)):
        if frac >= 0.7:
            return "ai"
        if frac >= 0.3:
            return "mixed"
        return "human"
    score = detector.get("score")
    if isinstance(score, (int, float)):
        if score >= 0.7:
            return "ai"
        if score >= 0.3:
            return "mixed"
        return "human"
    return "unscored"


def format_dwell_ms(ms: int | float | None) -> str:
    if not ms or ms <= 0:
        return "0s"
    total_seconds = ms / 1000.0
    if total_seconds < 60:
        return f"{total_seconds:.1f}s"
    minutes = int(total_seconds // 60)
    seconds = int(total_seconds % 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours = int(minutes // 60)
    minutes = int(minutes % 60)
    return f"{hours}h {minutes}m"


# ─────────────────────────── Overview ───────────────────────────

async def overview_counts(db) -> dict[str, Any]:
    obs_count = await db.observations.count_documents({})
    dwell_pipe = [
        {"$group": {"_id": None, "totalDwell": {"$sum": "$dwellMs"}}}
    ]
    dwell_res = await db.observations.aggregate(dwell_pipe).to_list(1)
    total_dwell_ms = dwell_res[0]["totalDwell"] if dwell_res else 0
    avg_dwell_ms = round(total_dwell_ms / obs_count) if obs_count > 0 else 0

    return {
        "participants": await db.participants.count_documents({}),
        "observations": obs_count,
        "interactions": await db.interactions.count_documents({}),
        "scored_posts": await db.scored_posts.count_documents({}),
        "dwell_logs": await db.dwell_logs.count_documents({}),
        "total_dwell_ms": total_dwell_ms,
        "avg_dwell_ms": avg_dwell_ms,
        "total_dwell_formatted": format_dwell_ms(total_dwell_ms),
        "avg_dwell_formatted": format_dwell_ms(avg_dwell_ms),
    }


async def score_distribution(db) -> dict[str, int]:
    """Counts of observations bucketed by tier (joined against scored_posts)."""
    pipeline = [
        {"$lookup": {
            "from": "scored_posts",
            "let": {"p": "$platform", "pid": "$postId"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$platform", "$$p"]},
                    {"$eq": ["$postId", "$$pid"]},
                ]}}},
                {"$project": {"detector": 1}},
            ],
            "as": "sp",
        }},
        {"$project": {"detector": {"$arrayElemAt": ["$sp.detector", 0]}}},
    ]
    counts = {"human": 0, "mixed": 0, "ai": 0, "unscored": 0}
    async for doc in db.observations.aggregate(pipeline):
        counts[tier_from_detector(doc.get("detector"))] += 1
    return counts


async def observations_per_day(db, days: int = 30) -> list[dict]:
    """Count of observations bucketed by day for the last N days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    pipeline = [
        {"$match": {"recordedAt": {"$gte": cutoff}}},
        {"$group": {
            "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$recordedAt"}},
            "count": {"$sum": 1},
        }},
        {"$sort": {"_id": 1}},
    ]
    out = []
    async for doc in db.observations.aggregate(pipeline):
        out.append({"date": doc["_id"], "count": doc["count"]})
    return out


# ─────────────────────────── Participants ───────────────────────────

async def list_participants(db, group: str | None = None, skip: int = 0, limit: int = 200) -> list[dict]:
    """List participants with derived counts (observation+interaction totals,
    dwell totals, last activity timestamp).
    """
    query = {}
    if group:
        query["group"] = group

    cursor = db.participants.find(query).sort("createdAt", -1).skip(skip).limit(limit)
    out = []
    async for p in cursor:
        pid = p.get("participantId") or str(p["_id"])
        obs_count = await db.observations.count_documents({"userId": pid})
        int_count = await db.interactions.count_documents({"userId": pid})
        last_obs = await db.observations.find_one(
            {"userId": pid}, sort=[("recordedAt", -1)], projection={"recordedAt": 1}
        )
        dwell_pipe = [
            {"$match": {"userId": pid}},
            {"$group": {"_id": None, "totalDwell": {"$sum": "$dwellMs"}}}
        ]
        dwell_res = await db.observations.aggregate(dwell_pipe).to_list(1)
        total_p_dwell = dwell_res[0]["totalDwell"] if dwell_res else 0
        avg_p_dwell = round(total_p_dwell / obs_count) if obs_count > 0 else 0

        out.append({
            "participantId": pid,
            "externalId": p.get("externalId"),
            "role": p.get("role", "participant"),
            "group": p.get("group", "unknown"),
            "createdAt": p.get("createdAt"),
            "observationCount": obs_count,
            "interactionCount": int_count,
            "totalDwellMs": total_p_dwell,
            "totalDwellFormatted": format_dwell_ms(total_p_dwell),
            "avgDwellMs": avg_p_dwell,
            "avgDwellFormatted": format_dwell_ms(avg_p_dwell),
            "lastSeen": last_obs.get("recordedAt") if last_obs else None,
        })
    return out


async def get_participant(db, participant_id: str) -> dict | None:
    p = await db.participants.find_one({"participantId": participant_id})
    if not p:
        return None
    obs_count = await db.observations.count_documents({"userId": participant_id})
    int_count = await db.interactions.count_documents({"userId": participant_id})
    dwell_pipe = [
        {"$match": {"userId": participant_id}},
        {"$group": {"_id": None, "totalDwell": {"$sum": "$dwellMs"}}}
    ]
    dwell_res = await db.observations.aggregate(dwell_pipe).to_list(1)
    total_p_dwell = dwell_res[0]["totalDwell"] if dwell_res else 0
    avg_p_dwell = round(total_p_dwell / obs_count) if obs_count > 0 else 0

    return {
        "participantId": p.get("participantId"),
        "externalId": p.get("externalId"),
        "role": p.get("role", "participant"),
        "group": p.get("group", "unknown"),
        "createdAt": p.get("createdAt"),
        "active": p.get("active", True),
        "observationCount": obs_count,
        "interactionCount": int_count,
        "totalDwellMs": total_p_dwell,
        "totalDwellFormatted": format_dwell_ms(total_p_dwell),
        "avgDwellMs": avg_p_dwell,
        "avgDwellFormatted": format_dwell_ms(avg_p_dwell),
    }



# ─────────────────────────── Observations / interactions browsers ───────────────────────────

def _build_obs_filter(
    user_id: str | None = None,
    platform: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict:
    q: dict[str, Any] = {}
    if user_id:
        q["userId"] = user_id
    if platform:
        q["platform"] = platform
    if since or until:
        q["recordedAt"] = {}
        if since:
            q["recordedAt"]["$gte"] = since
        if until:
            q["recordedAt"]["$lte"] = until
    return q


async def list_observations(
    db,
    user_id: str | None = None,
    platform: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    skip: int = 0,
    limit: int = 100,
    join_detector: bool = True,
) -> tuple[list[dict], int]:
    q = _build_obs_filter(user_id, platform, since, until)
    total = await db.observations.count_documents(q)
    cursor = db.observations.find(q).sort("recordedAt", -1).skip(skip).limit(limit)
    docs = []
    async for d in cursor:
        d["_id"] = str(d["_id"])
        d["dwellMs"] = d.get("dwellMs", 0)
        d["dwellCount"] = d.get("dwellCount", 0)
        d["dwellFormatted"] = format_dwell_ms(d["dwellMs"])
        docs.append(d)

    if join_detector and docs:
        keys = list({(d["platform"], d["postId"]) for d in docs})
        or_clause = [{"platform": p, "postId": pid} for p, pid in keys]
        idx: dict[tuple[str, str], dict] = {}
        async for s in db.scored_posts.find(
            {"$or": or_clause},
            projection={"platform": 1, "postId": 1, "detector": 1, "text": 1},
        ):
            idx[(s["platform"], s["postId"])] = s
        for d in docs:
            m = idx.get((d["platform"], d["postId"]))
            d["detector"] = m.get("detector") if m else None
            d["text"] = m.get("text") if m else None
            d["tier"] = tier_from_detector(d.get("detector"))
    return docs, total


async def list_interactions(
    db,
    user_id: str | None = None,
    platform: str | None = None,
    action: str | None = None,
    skip: int = 0,
    limit: int = 100,
) -> tuple[list[dict], int]:
    q: dict[str, Any] = {}
    if user_id:
        q["userId"] = user_id
    if platform:
        q["platform"] = platform
    if action:
        q["action"] = action
    total = await db.interactions.count_documents(q)
    cursor = db.interactions.find(q).sort("recordedAt", -1).skip(skip).limit(limit)
    docs = []
    async for d in cursor:
        d["_id"] = str(d["_id"])
        docs.append(d)
    return docs, total
