"""Database wipe / reset utility for Social AI Research (SAIR).

Clears participant and activity collections, ensuring indexes are recreated cleanly.
Requires explicit confirmation to prevent accidental loss.

Usage:
    python wipe_db.py
    python wipe_db.py --force
    python wipe_db.py --include-cache
"""

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

import db

load_dotenv()


async def wipe_database(mongo_uri: str, db_name: str, include_cache: bool = False, force: bool = False) -> None:
    client = AsyncIOMotorClient(mongo_uri)
    target_db = client[db_name]

    # Pre-wipe count check
    collections = ["participants", "observations", "interactions", "dwell_logs"]
    if include_cache:
        collections.append("scored_posts")

    counts_before = {}
    total_docs = 0
    for c in collections:
        count = await target_db[c].count_documents({})
        counts_before[c] = count
        total_docs += count

    print("=" * 60)
    print("⚠️  DANGER: SAIR DATABASE WIPE / CLEAN RESET")
    print("=" * 60)
    print(f"Target Database : {db_name}")
    print(f"MongoDB URI     : {mongo_uri}")
    print(f"Total documents to delete: {total_docs:,}")
    for c, cnt in counts_before.items():
        print(f"  - {c}: {cnt:,} documents")

    if not include_cache:
        cached_count = await target_db.scored_posts.count_documents({})
        print(f"  * Note: 'scored_posts' (Pangram cache: {cached_count:,} docs) will be PRESERVED.")
        print("    (Pass --include-cache if you also want to wipe cached Pangram scores).")

    print("=" * 60)

    if not force:
        confirm = input("Are you ABSOLUTELY sure? Type 'CONFIRM_WIPE' to proceed: ").strip()
        if confirm != "CONFIRM_WIPE":
            print("[x] Wipe cancelled. No data was deleted.")
            client.close()
            sys.exit(0)

    print("\n[*] Deleting collection contents...")
    for c in collections:
        res = await target_db[c].delete_many({})
        print(f"  -> Cleared {c}: {res.deleted_count:,} documents deleted.")

    print("[*] Rebuilding database indexes...")
    # Initialize db.state so _ensure_indexes works
    db.state.client = client
    db.state.db = target_db
    await db._ensure_indexes()
    print("[✓] Indexes successfully re-verified and rebuilt.")

    print("\n[✓] Database wipe complete! System is reset and ready for clean participant intake.")
    client.close()


def main():
    parser = argparse.ArgumentParser(description="Wipe SAIR MongoDB Database")
    parser.add_argument("--uri", default=os.environ.get("MONGO_URI", "mongodb://localhost:27017"), help="MongoDB URI")
    parser.add_argument("--db", default=os.environ.get("MONGO_DB", "sair"), help="Database name")
    parser.add_argument("--include-cache", action="store_true", help="Also wipe cached Pangram scores")
    parser.add_argument("--force", action="store_true", help="Bypass interactive confirmation prompt")
    args = parser.parse_args()

    asyncio.run(wipe_database(args.uri, args.db, include_cache=args.include_cache, force=args.force))


if __name__ == "__main__":
    main()

