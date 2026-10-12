import os

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import IndexModel, ASCENDING


class _Holder:
    client: AsyncIOMotorClient | None = None
    db = None


state = _Holder()


async def connect() -> None:
    uri = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
    name = os.environ.get("MONGO_DB", "sair")
    state.client = AsyncIOMotorClient(uri)
    state.db = state.client[name]
    await _ensure_indexes()


async def disconnect() -> None:
    if state.client is not None:
        state.client.close()
        state.client = None
        state.db = None


async def _ensure_indexes() -> None:
    await state.db.scored_posts.create_indexes([
        IndexModel(
            [("platform", ASCENDING), ("postId", ASCENDING)],
            unique=True,
            name="platform_postId_unique",
        ),
        IndexModel([("textHash", ASCENDING)], name="textHash"),
    ])
    obs = state.db.observations
    # If an earlier deploy created the non-unique variant of this compound
    # index, drop it so create_indexes can install the unique one.
    target_key = [("userId", 1), ("platform", 1), ("postId", 1)]
    for name, info in (await obs.index_information()).items():
        if info.get("key") == target_key and not info.get("unique"):
            await obs.drop_index(name)
            break
    await obs.create_indexes([
        IndexModel([("userId", ASCENDING)], name="userId"),
        IndexModel(
            [("userId", ASCENDING), ("platform", ASCENDING), ("postId", ASCENDING)],
            unique=True,
            name="userId_platform_postId_unique",
        ),
        IndexModel([("recordedAt", ASCENDING)], name="recordedAt"),
    ])
    await state.db.participants.create_indexes([
        IndexModel([("token", ASCENDING)], unique=True, name="token_unique"),
        IndexModel([("participantId", ASCENDING)], unique=True, name="participantId_unique"),
    ])
    # Sparse+unique: not all participants have an externalId, but when they
    # do, it must be unique. If existing data has duplicates from earlier runs,
    # MongoDB will reject the unique constraint. In that case, fall back to a
    # non-unique sparse index so startup doesn't crash.
    try:
        await state.db.participants.drop_index("externalId")
    except Exception:
        pass
    try:
        await state.db.participants.create_indexes([
            IndexModel(
                [("externalId", ASCENDING)],
                sparse=True,
                unique=True,
                name="externalId_unique",
            ),
        ])
    except Exception as e:
        import logging
        logging.getLogger("uvicorn.error").warning(
            "Could not build unique externalId index due to existing duplicate records; falling back to non-unique sparse index: %s", e
        )
        await state.db.participants.create_indexes([
            IndexModel([("externalId", ASCENDING)], sparse=True, name="externalId"),
        ])
    # Interactions: append-only event log. No unique constraints — toggle
    # sequences (like → unlike → like) are valid and each event is a separate
    # row. Indexes optimize the common research queries:
    #   "all interactions by this user"   →  userId
    #   "interactions on a specific post" →  (userId, platform, postId)
    #   "time-series per participant"     →  (userId, occurredAt)
    #   "global counts by action"         →  (platform, action)
    await state.db.interactions.create_indexes([
        IndexModel([("userId", ASCENDING)], name="userId"),
        IndexModel(
            [("userId", ASCENDING), ("platform", ASCENDING), ("postId", ASCENDING)],
            name="userId_platform_postId",
        ),
        IndexModel(
            [("userId", ASCENDING), ("occurredAt", ASCENDING)],
            name="userId_occurredAt",
        ),
        IndexModel(
            [("platform", ASCENDING), ("action", ASCENDING)],
            name="platform_action",
        ),
    ])
    # Dwell logs: granular event log of dwell sessions / periodic flushes.
    await state.db.dwell_logs.create_indexes([
        IndexModel([("userId", ASCENDING)], name="userId"),
        IndexModel(
            [("userId", ASCENDING), ("platform", ASCENDING), ("postId", ASCENDING)],
            name="userId_platform_postId",
        ),
        IndexModel([("recordedAt", ASCENDING)], name="recordedAt"),
        IndexModel([("platform", ASCENDING)], name="platform"),
    ])

