"""Mongo query helpers for the admin dashboard.

All functions take the `db` (motor database) as their first arg so they're
trivially testable and don't reach into the global state from the regular
backend module.
"""

import os
import zoneinfo
from datetime import datetime, timedelta, timezone
from typing import Any
from bson import ObjectId


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


# ─────────────────────────── Participant Activity & Compliance ───────────────────────────

async def participant_compliance_stats(db, participant: dict) -> dict[str, Any]:
    """Calculate daily activity and compliance metrics for a participant.
    
    All participant data is preserved continuously across all days. Days are
    bucketed into the participant's local US timezone. Evaluates:
    1. First 7-Day Compliance (target: at least 10 minutes active per day for 5/7 days).
    2. Total Active Days across the entire participation duration.
    """
    pid = participant.get("participantId") or str(participant["_id"])
    tz_str = participant.get("timezone") or "America/New_York"
    tz = resolve_tz(tz_str)

    # Determine registration / join date in UTC and local timezone
    # Prefer enrolledAt, then createdAt, or now
    join_dt = participant.get("enrolledAt") or participant.get("createdAt")
    if join_dt:
        if join_dt.tzinfo is None:
            join_dt = join_dt.replace(tzinfo=timezone.utc)
    else:
        join_dt = datetime.now(timezone.utc)

    now_local = datetime.now(timezone.utc).astimezone(tz)
    today_date = now_local.date()

    # The official registration calendar date (as recorded in the database / Prolific in UTC)
    reg_calendar_date = join_dt.astimezone(timezone.utc).date()
    created_local_date = join_dt.astimezone(tz).date()

    # Use the official registration date; timezone offset must NEVER push
    # the participant's start date back to a calendar day before they registered!
    start_date = max(created_local_date, reg_calendar_date)
    start_date = min(start_date, today_date)

    daily_dwell_ms: dict[str, int] = {}
    daily_interactions: dict[str, int] = {}

    # 1. Aggregate all dwell logs across all time
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

    # 2. Aggregate all interactions across all time
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

    start_date_str = start_date.strftime("%Y-%m-%d")

    # If any activity occurred around registration that fell into a date before start_date
    # due to timezone conversion, roll it into Day 1 (start_date) so no activity is lost
    early_dwell = sum(v for k, v in daily_dwell_ms.items() if k < start_date_str)
    if early_dwell > 0:
        daily_dwell_ms[start_date_str] = daily_dwell_ms.get(start_date_str, 0) + early_dwell
        for k in list(daily_dwell_ms.keys()):
            if k < start_date_str:
                del daily_dwell_ms[k]

    early_ints = sum(v for k, v in daily_interactions.items() if k < start_date_str)
    if early_ints > 0:
        daily_interactions[start_date_str] = daily_interactions.get(start_date_str, 0) + early_ints
        for k in list(daily_interactions.keys()):
            if k < start_date_str:
                del daily_interactions[k]

    # Days elapsed strictly since the participant joined up to today
    days_elapsed = max(1, (today_date - start_date).days + 1)
    study_dates = [start_date + timedelta(days=i) for i in range(days_elapsed)]

    # Build the multi-day breakdown matrix for all elapsed days
    matrix = []
    first7_qualifying_count = 0
    total_qualifying_count = 0

    for idx, d in enumerate(study_dates):
        key = d.strftime("%Y-%m-%d")
        ms = daily_dwell_ms.get(key, 0)
        mins = round(ms / 60000.0, 1)
        ints = daily_interactions.get(key, 0)

        # Qualification rule: at least 10 minutes (600,000 ms) of active time
        qualifies = ms >= 600000
        if qualifies:
            total_qualifying_count += 1

        is_today = d == today_date
        is_first7 = idx < 7

        if is_first7 and qualifies:
            first7_qualifying_count += 1

        matrix.append({
            "day": idx + 1,
            "date": key,
            "dateFormatted": d.strftime("%b %d"),
            "dwellMs": ms,
            "activeMinutes": mins,
            "interactions": ints,
            "qualifies": qualifies,
            "isFirst7Days": is_first7,
            "isFuture": False,
            "isToday": is_today,
        })

    today_key = today_date.strftime("%Y-%m-%d")
    today_active_mins = round(daily_dwell_ms.get(today_key, 0) / 60000.0, 1)
    today_interactions = daily_interactions.get(today_key, 0)

    # Compliance status evaluation:
    # Target: 5 qualifying days in initial 7-day period
    if first7_qualifying_count >= 5:
        status = "completed"
        status_label = f"Completed ({first7_qualifying_count}/7)"
        badge_class = "bg-emerald-950/60 text-emerald-300 border-emerald-600/50"
    elif days_elapsed < 7:
        # Participant is actively progressing within their 7-day window
        if first7_qualifying_count >= 1 and (first7_qualifying_count / days_elapsed) >= 0.5:
            status = "on_track"
            status_label = f"Day {days_elapsed} of 7 · On Track"
            badge_class = "bg-blue-950/50 text-blue-300 border-blue-700/40"
        else:
            status = "in_progress"
            status_label = f"Day {days_elapsed} of 7"
            badge_class = "bg-zinc-800 text-zinc-300 border-zinc-700"
    else:
        # Full 7+ days have elapsed without hitting 5 days
        status = "incomplete"
        status_label = f"Incomplete ({first7_qualifying_count}/7)"
        badge_class = "bg-amber-950/50 text-amber-300 border-amber-800/40"

    # Rolling 7-day window (last 7 calendar days up to today)
    rolling_7_dates = [today_date - timedelta(days=i) for i in range(7)]
    rolling_7_qualifying = sum(
        1 for rd in rolling_7_dates if daily_dwell_ms.get(rd.strftime("%Y-%m-%d"), 0) >= 600000
    )

    return {
        "timezone": tz_str,
        "startDate": start_date.isoformat(),
        "startDateFormatted": start_date.strftime("%b %d"),
        "qualifyingDays": first7_qualifying_count,
        "totalQualifyingDays": total_qualifying_count,
        "daysElapsed": days_elapsed,
        "totalStudyDays": days_elapsed,
        "complianceRatio": f"{first7_qualifying_count}/5",
        "rollingComplianceRatio": f"{rolling_7_qualifying}/7",
        "status": status,
        "statusLabel": status_label,
        "badgeClass": badge_class,
        "todayActiveMinutes": today_active_mins,
        "todayInteractions": today_interactions,
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

async def get_distinct_cohorts(db) -> list[int]:
    """Get all distinct cohort numbers in the database, always including 1."""
    cohorts = {1}
    try:
        vals = await db.participants.distinct("cohort")
        for v in vals:
            if v is not None:
                try:
                    cohorts.add(int(v))
                except (ValueError, TypeError):
                    pass
    except Exception:
        pass
    env_c = os.environ.get("CURRENT_COHORT", os.environ.get("CURRENT_ROUND", "1"))
    try:
        cohorts.add(int(env_c))
    except (ValueError, TypeError):
        pass
    return sorted(list(cohorts))


async def list_participants(
    db, group: str | None = None, cohort: int | None = None, status: str | None = None, search: str | None = None, skip: int = 0, limit: int = 200, **kwargs
) -> list[dict]:
    """List participants with derived counts, dwell totals, and compliance metrics."""
    query: dict[str, Any] = {}
    if group:
        query["group"] = group
    if status == "blocked":
        query["blocked"] = True
    elif status == "active":
        query["$or"] = [{"blocked": False}, {"blocked": {"$exists": False}}]
    if cohort is not None and str(cohort).strip() != "":
        try:
            c_int = int(cohort)
            if c_int == 1:
                query["$or"] = [{"cohort": 1}, {"cohort": {"$exists": False}}, {"round": 1}]
            else:
                query["$or"] = [{"cohort": c_int}, {"round": c_int}]
        except (ValueError, TypeError):
            pass
    if search and str(search).strip():
        s = str(search).strip()
        import re
        regex = {"$regex": re.escape(s), "$options": "i"}
        search_filter = [
            {"externalId": regex},
            {"participantId": regex},
            {"timezone": regex},
        ]
        if "$or" in query:
            query = {"$and": [{"$or": query["$or"]}, {"$or": search_filter}]}
        else:
            query["$or"] = search_filter

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

        # Calculate active days compliance
        compliance = await participant_compliance_stats(db, p)

        c_val = p.get("cohort", p.get("round", 1))
        out.append({
            "participantId": pid,
            "externalId": p.get("externalId"),
            "role": p.get("role", "participant"),
            "group": p.get("group", "unknown"),
            "cohort": int(c_val) if c_val is not None else 1,
            "timezone": p.get("timezone", "America/New_York"),
            "createdAt": p.get("createdAt"),
            "enrolledAt": p.get("enrolledAt"),
            "active": p.get("active", True),
            "blocked": bool(p.get("blocked", False)),
            "blockedAt": p.get("blockedAt"),
            "blockedReason": p.get("blockedReason", ""),
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

    c_val = p.get("cohort", p.get("round", 1))
    return {
        "participantId": p.get("participantId"),
        "externalId": p.get("externalId"),
        "role": p.get("role", "participant"),
        "group": p.get("group", "unknown"),
        "cohort": int(c_val) if c_val is not None else 1,
        "timezone": p.get("timezone", "America/New_York"),
        "createdAt": p.get("createdAt"),
        "enrolledAt": p.get("enrolledAt"),
        "active": p.get("active", True),
        "blocked": bool(p.get("blocked", False)),
        "blockedAt": p.get("blockedAt"),
        "blockedReason": p.get("blockedReason", ""),
        "observationCount": obs_count,
        "interactionCount": int_count,
        "totalDwellMs": total_p_dwell,
        "totalDwellFormatted": format_dwell_ms(total_p_dwell),
        "avgDwellMs": avg_p_dwell,
        "avgDwellFormatted": format_dwell_ms(avg_p_dwell),
        "compliance": compliance,
    }


async def update_participant(db, participant_id: str, updates: dict) -> bool:
    """Update participant details (group, role, cohort, externalId, timezone, active, blocked)."""
    allowed = {"group", "role", "cohort", "externalId", "timezone", "active", "blocked", "blockedReason", "blockedAt"}
    set_fields = {k: v for k, v in updates.items() if k in allowed}
    if "cohort" in set_fields:
        try:
            set_fields["cohort"] = int(set_fields["cohort"])
        except (ValueError, TypeError):
            pass
    if not set_fields:
        return False
    res = await db.participants.update_one(
        {"participantId": participant_id},
        {"$set": set_fields}
    )
    return res.matched_count > 0


async def block_identifiers(db, identifiers: list[str], reason: str = "") -> dict:
    """Block participants by Prolific ID (externalId) or Mongo ID (_id/participantId).
    
    Guarantees that:
    1. Any existing participant matching the identifier is permanently marked blocked.
    2. Any identifier (even if not yet registered) is persistently recorded into the
       `blocked_participants` collection so future requests/registrations are blocked.
    """
    now = datetime.now(timezone.utc)
    cleaned = []
    for raw in identifiers:
        s = str(raw).strip().strip('"').strip("'").strip("\ufeff")
        if s and s not in cleaned:
            cleaned.append(s)

    matched_pids = set()
    new_blocked_entries = 0
    details = []

    for ident in cleaned:
        clauses = [
            {"externalId": ident},
            {"participantId": ident},
        ]
        if len(ident) == 24:
            try:
                clauses.append({"_id": ObjectId(ident)})
            except Exception:
                pass

        matched = await db.participants.find({"$or": clauses}).to_list(20)
        is_hex24 = len(ident) == 24 and all(c in "0123456789abcdefABCDEF" for c in ident)
        ident_type = "mongo_id" if is_hex24 else ("participant_id" if ident.startswith("u-") else "prolific_id")

        if matched:
            for p in matched:
                pid = p.get("participantId") or str(p["_id"])
                matched_pids.add(pid)
                await db.participants.update_one(
                    {"_id": p["_id"]},
                    {"$set": {"blocked": True, "blockedAt": now, "blockedReason": reason or "Admin blocklist"}}
                )
                # Save into blocked_participants collection
                await db.blocked_participants.update_one(
                    {"identifier": ident},
                    {"$set": {
                        "identifier": ident,
                        "identifierType": ident_type,
                        "participantId": pid,
                        "externalId": p.get("externalId"),
                        "mongoId": str(p["_id"]),
                        "reason": reason or "Admin blocklist",
                        "blockedAt": now,
                        "active": True
                    }},
                    upsert=True
                )
                # Also index their externalId if different
                if p.get("externalId") and p["externalId"] != ident:
                    await db.blocked_participants.update_one(
                        {"identifier": p["externalId"]},
                        {"$set": {
                            "identifier": p["externalId"],
                            "identifierType": "prolific_id",
                            "participantId": pid,
                            "externalId": p["externalId"],
                            "mongoId": str(p["_id"]),
                            "reason": reason or "Admin blocklist",
                            "blockedAt": now,
                            "active": True
                        }},
                        upsert=True
                    )
                # Also index their participantId if different
                if pid and pid != ident:
                    await db.blocked_participants.update_one(
                        {"identifier": pid},
                        {"$set": {
                            "identifier": pid,
                            "identifierType": "participant_id",
                            "participantId": pid,
                            "externalId": p.get("externalId"),
                            "mongoId": str(p["_id"]),
                            "reason": reason or "Admin blocklist",
                            "blockedAt": now,
                            "active": True
                        }},
                        upsert=True
                    )
                details.append({
                    "identifier": ident,
                    "participantId": pid,
                    "externalId": p.get("externalId"),
                    "status": "matched_and_blocked"
                })
        else:
            # Not yet in participants table — save to persistent blocklist
            await db.blocked_participants.update_one(
                {"identifier": ident},
                {"$set": {
                    "identifier": ident,
                    "identifierType": ident_type,
                    "participantId": None,
                    "externalId": ident if ident_type == "prolific_id" else None,
                    "reason": reason or "Admin blocklist",
                    "blockedAt": now,
                    "active": True
                }},
                upsert=True
            )
            new_blocked_entries += 1
            details.append({
                "identifier": ident,
                "participantId": None,
                "status": "saved_to_blocklist"
            })

    return {
        "total_parsed": len(identifiers),
        "unique_identifiers": len(cleaned),
        "matched_participants_count": len(matched_pids),
        "new_blocklist_entries": new_blocked_entries,
        "details": details,
    }


async def unblock_identifier(db, identifier: str) -> bool:
    """Unblock a participant by identifier (Prolific ID, Mongo ID, or participantId)."""
    s = str(identifier).strip()
    clauses = [
        {"externalId": s},
        {"participantId": s},
    ]
    if len(s) == 24:
        try:
            clauses.append({"_id": ObjectId(s)})
        except Exception:
            pass

    # Unset blocked on matching participants
    p_res = await db.participants.update_many(
        {"$or": clauses},
        {"$set": {"blocked": False}, "$unset": {"blockedReason": "", "blockedAt": ""}}
    )

    # Remove from blocked_participants
    b_res = await db.blocked_participants.delete_many({
        "$or": [
            {"identifier": s},
            {"externalId": s},
            {"participantId": s},
            {"mongoId": s},
        ]
    })
    return (p_res.modified_count > 0) or (b_res.deleted_count > 0)


async def list_blocked(db, skip: int = 0, limit: int = 200, search: str | None = None) -> tuple[list[dict], int]:
    """List distinct blocked entities (deduplicating multiple alias identifiers per participant)."""
    q = {}
    if search and search.strip():
        import re
        rgx = {"$regex": re.escape(search.strip()), "$options": "i"}
        q = {"$or": [{"identifier": rgx}, {"participantId": rgx}, {"externalId": rgx}, {"reason": rgx}]}
    cursor = db.blocked_participants.find(q).sort("blockedAt", -1)
    
    seen_entities = set()
    out = []
    async for doc in cursor:
        pid = doc.get("participantId")
        ext = doc.get("externalId")
        ident = doc.get("identifier")
        # Deduplication key: one row per distinct participant, or per distinct unenrolled identifier
        entity_key = f"pid:{pid}" if pid else (f"ext:{ext}" if ext else f"id:{ident}")
        if entity_key in seen_entities:
            continue
        seen_entities.add(entity_key)

        primary_id = ext or ident or pid
        out.append({
            "identifier": ident,
            "displayIdentifier": primary_id,
            "identifierType": doc.get("identifierType", "unknown"),
            "participantId": pid,
            "externalId": ext,
            "mongoId": doc.get("mongoId"),
            "reason": doc.get("reason", "Admin blocklist"),
            "blockedAt": doc.get("blockedAt"),
            "isEnrolled": bool(pid),
        })

    total = len(out)
    return out[skip : skip + limit], total


async def count_blocked(db) -> dict:
    """Get counts of distinct blocked participants and total pending entries."""
    cursor = db.blocked_participants.find({})
    seen = set()
    enrolled_count = 0
    unenrolled_count = 0
    async for doc in cursor:
        pid = doc.get("participantId")
        ext = doc.get("externalId")
        ident = doc.get("identifier")
        entity_key = f"pid:{pid}" if pid else (f"ext:{ext}" if ext else f"id:{ident}")
        if entity_key in seen:
            continue
        seen.add(entity_key)
        if pid:
            enrolled_count += 1
        else:
            unenrolled_count += 1

    return {
        "total": len(seen),
        "enrolled": enrolled_count,
        "unenrolled": unenrolled_count,
        "total_blocklist_entries": len(seen),
        "blocked_participants": enrolled_count,
    }



async def delete_participant(db, participant_id: str, cascade: bool = True) -> dict:
    """Delete a participant, optionally cascading deletion to observations, dwell logs, and interactions."""
    p_res = await db.participants.delete_one({"participantId": participant_id})
    res: dict[str, Any] = {"participantDeleted": p_res.deleted_count > 0}
    if cascade:
        obs_res = await db.observations.delete_many({"userId": participant_id})
        int_res = await db.interactions.delete_many({"userId": participant_id})
        dwell_res = await db.dwell_logs.delete_many({"userId": participant_id})
        res["observationsDeleted"] = obs_res.deleted_count
        res["interactionsDeleted"] = int_res.deleted_count
        res["dwellLogsDeleted"] = dwell_res.deleted_count
    return res


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
