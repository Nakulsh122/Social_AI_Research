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
        # Sparse: not all participants have an externalId (older rows from
        # before the popup setup flow won't, and the field stays optional).
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
