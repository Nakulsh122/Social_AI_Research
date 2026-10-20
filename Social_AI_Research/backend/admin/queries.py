"""Mongo query helpers for the admin dashboard.

All functions take the `db` (motor database) as their first arg so they're
trivially testable and don't reach into the global state from the regular
backend module.
"""

import zoneinfo
from datetime import datetime, timedelta, timezone
from typing import Any


# ─────────────────────────── Timezone helper ───────────────────────────

def resolve_tz(tz_name: str | None) -> zoneinfo.ZoneInfo:
    """Resolve an IANA timezone string (defaulting to US Eastern)."""
    if not tz_name:
        return zoneinfo.ZoneInfo("America/New_York")
    try:
        return zoneinfo.ZoneInfo(tz_name.strip())
    except Exception:
        return zoneinfo.ZoneInfo("America/New_York")


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


# ─────────────────────────── Participant 5/7 Days Compliance ───────────────────────────

async def participant_compliance_stats(db, participant: dict) -> dict[str, Any]:
    """Calculate 5/7 active days compliance for a participant.
    
    A participant must have at least 10 minutes of active activity per day for
    at least 5 days out of 7, bucketed in their local US timezone.
    """
    pid = participant.get("participantId") or str(participant["_id"])
    tz_str = participant.get("timezone") or "America/New_York"
    tz = resolve_tz(tz_str)

    # Determine registration date in local timezone
    created_at = participant.get("createdAt")
    if created_at:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        created_local = created_at.astimezone(tz)
    else:
        created_local = datetime.now(timezone.utc).astimezone(tz)

    start_date = created_local.date()
    now_local = datetime.now(timezone.utc).astimezone(tz)
    today_date = now_local.date()

    # The 7 study calendar dates (Day 1 to Day 7)
    study_dates = [start_date + timedelta(days=i) for i in range(7)]

    # Initialize aggregators
    daily_dwell_ms: dict[str, int] = {d.strftime("%Y-%m-%d"): 0 for d in study_dates}
    daily_interactions: dict[str, int] = {d.strftime("%Y-%m-%d"): 0 for d in study_dates}

    # 1. Aggregate dwell time (active focal reading on posts)
    dwell_cursor = db.dwell_logs.find(
        {"userId": pid},
        projection={"dwellMs": 1, "recordedAt": 1, "occurredAt": 1}
    )
    async for doc in dwell_cursor:
        rec = doc.get("recordedAt")
        if rec:
            if rec.tzinfo is None:
                rec = rec.replace(tzinfo=timezone.utc)
            date_key = rec.astimezone(tz).strftime("%Y-%m-%d")
        elif doc.get("occurredAt"):
            dt = datetime.fromtimestamp(doc["occurredAt"] / 1000.0, tz=timezone.utc)
            date_key = dt.astimezone(tz).strftime("%Y-%m-%d")
        else:
            continue

        daily_dwell_ms[date_key] = daily_dwell_ms.get(date_key, 0) + doc.get("dwellMs", 0)

    # 2. Aggregate active engagement clicks (likes, comments, reposts, send)
    int_cursor = db.interactions.find(
        {"userId": pid},
        projection={"action": 1, "recordedAt": 1, "occurredAt": 1}
    ).sort("recordedAt", -1)
    
    last_active_action = None
    last_active_time = None
    
    async for doc in int_cursor:
        if not last_active_action:
            last_active_action = doc.get("action")
            rec = doc.get("recordedAt")
            if rec:
                if rec.tzinfo is None:
                    rec = rec.replace(tzinfo=timezone.utc)
                last_active_time = rec

        rec = doc.get("recordedAt")
        if rec:
            if rec.tzinfo is None:
                rec = rec.replace(tzinfo=timezone.utc)
            date_key = rec.astimezone(tz).strftime("%Y-%m-%d")
        elif doc.get("occurredAt"):
            dt = datetime.fromtimestamp(doc["occurredAt"] / 1000.0, tz=timezone.utc)
            date_key = dt.astimezone(tz).strftime("%Y-%m-%d")
        else:
            continue

        daily_interactions[date_key] = daily_interactions.get(date_key, 0) + 1

    # Build the 7-day breakdown matrix
    matrix = []
    qualifying_days_count = 0
    days_elapsed = 0

    for idx, d in enumerate(study_dates):
        key = d.strftime("%Y-%m-%d")
        ms = daily_dwell_ms.get(key, 0)
        mins = round(ms / 60000.0, 1)
        ints = daily_interactions.get(key, 0)

        # Qualification rule: at least 10 minutes (600,000 ms) of active time
        qualifies = ms >= 600000
        if qualifies:
            qualifying_days_count += 1

        is_future = d > today_date
        is_today = d == today_date
        if not is_future:
            days_elapsed += 1

        matrix.append({
            "day": idx + 1,
            "date": key,
            "dateFormatted": d.strftime("%b %d"),
            "dwellMs": ms,
            "activeMinutes": mins,
            "interactions": ints,
            "qualifies": qualifies,
            "isFuture": is_future,
            "isToday": is_today,
        })

    today_key = today_date.strftime("%Y-%m-%d")
    today_active_mins = round(daily_dwell_ms.get(today_key, 0) / 60000.0, 1)
    today_interactions = daily_interactions.get(today_key, 0)

    # Compliance status evaluation (Target: >= 5 out of 7 days)
    remaining_days = max(0, 7 - days_elapsed)
    if qualifying_days_count >= 5:
        status = "completed"
        status_label = "Completed (5+/7)"
        badge_class = "bg-human/15 text-human border-human/30"
    elif qualifying_days_count + remaining_days < 5:
        status = "incomplete"
        status_label = "Incomplete (<5)"
        badge_class = "bg-ai/15 text-ai border-ai/30"
    elif days_elapsed > 0 and (qualifying_days_count / days_elapsed) >= (5.0 / 7.0):
        status = "on_track"
        status_label = "On Track"
        badge_class = "bg-blue-500/15 text-blue-400 border-blue-500/30"
    else:
        status = "at_risk"
        status_label = "At Risk"
        badge_class = "bg-amber-500/15 text-amber-400 border-amber-500/30"

    return {
        "timezone": tz_str,
        "qualifyingDays": qualifying_days_count,
        "totalStudyDays": 7,
        "complianceRatio": f"{qualifying_days_count}/7",
        "status": status,
        "statusLabel": status_label,
        "badgeClass": badge_class,
        "todayActiveMinutes": today_active_mins,
        "todayInteractions": today_interactions,
        "daysElapsed": min(7, days_elapsed),
        "lastActiveAction": last_active_action,
        "lastActiveTime": last_active_time,
        "matrix": matrix,
    }


# ─────────────────────────── Recent Activity Log ───────────────────────────

async def recent_active_events(db, limit: int = 25) -> list[dict]:
    """Fetch the latest participant active engagements (likes, comments, reposts, send)."""
    cursor = db.interactions.find().sort("recordedAt", -1).limit(limit)
    events = []
    participant_cache: dict[str, dict] = {}

    async for item in cursor:
        uid = item.get("userId", "")
        if uid and uid not in participant_cache:
            p = await db.participants.find_one(
                {"participantId": uid},
                projection={"externalId": 1, "group": 1, "timezone": 1}
            )
            participant_cache[uid] = p or {}
        p_info = participant_cache.get(uid, {})

        tz = resolve_tz(p_info.get("timezone"))
        rec = item.get("recordedAt")
        if rec and rec.tzinfo is None:
            rec = rec.replace(tzinfo=timezone.utc)

        local_rec = rec.astimezone(tz) if rec else None

        # Friendly action formatting
        action = item.get("action", "")
        action_labels = {
            "like_click": ("Liked a post", "bg-rose-500/15 text-rose-400 border-rose-500/30"),
            "comment_click": ("Commented", "bg-sky-500/15 text-sky-400 border-sky-500/30"),
            "repost_click": ("Reposted", "bg-emerald-500/15 text-emerald-400 border-emerald-500/30"),
            "send_click": ("Shared/Sent", "bg-purple-500/15 text-purple-400 border-purple-500/30"),
        }
        label, badge = action_labels.get(action, (action.replace("_", " ").title(), "bg-zinc-800 text-zinc-300 border-zinc-700"))

        events.append({
            "id": str(item.get("_id", "")),
            "userId": uid,
            "externalId": p_info.get("externalId"),
            "group": p_info.get("group", "unknown"),
            "platform": item.get("platform", "linkedin"),
            "postId": item.get("postId", ""),
            "action": action,
            "actionLabel": label,
            "badgeClass": badge,
            "recordedAt": rec,
            "recordedAtLocal": local_rec.strftime("%b %d · %H:%M %Z") if local_rec else "—",
        })
    return events


# ─────────────────────────── Participants ───────────────────────────

async def list_participants(db, group: str | None = None, skip: int = 0, limit: int = 200) -> list[dict]:
    """List participants with derived counts, dwell totals, and 5/7 compliance metrics."""
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

        # Calculate 5/7 active days compliance
        compliance = await participant_compliance_stats(db, p)

        out.append({
            "participantId": pid,
            "externalId": p.get("externalId"),
            "role": p.get("role", "participant"),
            "group": p.get("group", "unknown"),
            "timezone": p.get("timezone", "America/New_York"),
            "createdAt": p.get("createdAt"),
            "observationCount": obs_count,
            "interactionCount": int_count,
            "totalDwellMs": total_p_dwell,
            "totalDwellFormatted": format_dwell_ms(total_p_dwell),
            "avgDwellMs": avg_p_dwell,
            "avgDwellFormatted": format_dwell_ms(avg_p_dwell),
            "lastSeen": last_obs.get("recordedAt") if last_obs else None,
            "compliance": compliance,
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

    compliance = await participant_compliance_stats(db, p)

    return {
        "participantId": p.get("participantId"),
        "externalId": p.get("externalId"),
        "role": p.get("role", "participant"),
        "group": p.get("group", "unknown"),
        "timezone": p.get("timezone", "America/New_York"),
        "createdAt": p.get("createdAt"),
        "active": p.get("active", True),
        "observationCount": obs_count,
        "interactionCount": int_count,
        "totalDwellMs": total_p_dwell,
        "totalDwellFormatted": format_dwell_ms(total_p_dwell),
        "avgDwellMs": avg_p_dwell,
        "avgDwellFormatted": format_dwell_ms(avg_p_dwell),
        "compliance": compliance,
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
        d["hasAnomaly"] = bool(d.get("hasAnomaly") or d["dwellMs"] >= 120000)
        sessions = d.get("dwellSessions") or []
        session_strs = [format_dwell_ms(s.get("ms", 0)) for s in sessions if isinstance(s, dict)]
        d["sessionsSummary"] = ", ".join(session_strs) if session_strs else ""
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
