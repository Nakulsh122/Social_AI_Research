"""Hourly data export utility for Social AI Research (SAIR).

Exports all study collections and tabular reports into a single zip archive named `exports.zip`.
The output file is atomically replaced on every run, rather than accumulating multiple archives.

By default, the backend server runs this export automatically in the background every hour.
It can also be invoked as a standalone script:

Usage:
    # Run once and exit:
    python hourly_export.py --once

    # Run standalone continuous hourly daemon (default interval 3600 seconds):
    python hourly_export.py

    # Custom options:
    python hourly_export.py --uri mongodb://localhost:27017 --db sair --out exports.zip --interval 3600
"""

import argparse
import asyncio
import csv
import io
import json
import logging
import os
import signal
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bson import ObjectId
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv()

logger = logging.getLogger("hourly_export")


def json_serializer(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    return str(obj)


async def export_database(db: Any, out_path: Path, db_name: str | None = None) -> Path:
    """Export all collections from a Motor database to a single replaced zip archive."""
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temp_zip = out_path.parent / f"{out_path.name}.tmp"

    db_name = db_name or getattr(db, "name", "sair")
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()
    logger.info(f"[Hourly Export] Exporting database '{db_name}' -> {out_path.name}...")

    manifest: dict[str, Any] = {
        "exportTimestamp": now_iso,
        "database": db_name,
        "filename": out_path.name,
        "collections": {},
    }

    try:
        with zipfile.ZipFile(temp_zip, "w", zipfile.ZIP_DEFLATED) as zipf:
            # ──────────────── 1. Participants (JSON + CSV) ────────────────
            p_cursor = db.participants.find()
            participants = [p async for p in p_cursor]
            manifest["collections"]["participants"] = len(participants)

            # JSON (redacting token for safety, but keeping all researcher metadata)
            safe_participants = []
            for p in participants:
                doc = dict(p)
                doc.pop("token", None)
                safe_participants.append(doc)
            zipf.writestr("participants.json", json.dumps(safe_participants, default=json_serializer, indent=2))

            # CSV
            p_buf = io.StringIO()
            p_writer = csv.writer(p_buf)
            p_writer.writerow([
                "participantId", "externalId", "role", "group", "cohort",
                "timezone", "active", "blocked", "blockedReason", "createdAt", "enrolledAt"
            ])
            for p in participants:
                c_at = p.get("createdAt")
                e_at = p.get("enrolledAt")
                p_writer.writerow([
                    p.get("participantId") or str(p.get("_id", "")),
                    p.get("externalId") or "",
                    p.get("role", "participant"),
                    p.get("group", ""),
                    p.get("cohort", p.get("round", 1)),
                    p.get("timezone", "America/New_York"),
                    p.get("active", True),
                    p.get("blocked", False),
                    p.get("blockedReason", ""),
                    c_at.isoformat() if hasattr(c_at, "isoformat") else str(c_at or ""),
                    e_at.isoformat() if hasattr(e_at, "isoformat") else str(e_at or ""),
                ])
            zipf.writestr("participants.csv", p_buf.getvalue())

            # ──────────────── 2. Observations (JSON + CSV) ────────────────
            o_cursor = db.observations.find()
            observations = [o async for o in o_cursor]
            manifest["collections"]["observations"] = len(observations)
            zipf.writestr("observations.json", json.dumps(observations, default=json_serializer, indent=2))

            o_buf = io.StringIO()
            o_writer = csv.writer(o_buf)
            o_writer.writerow([
                "userId", "platform", "postId", "url", "author", "authorDisplayName",
                "authorVerified", "authorHeadline", "isPromoted", "postedAt",
                "seenAt", "badgeShown", "dwellMs", "dwellCount", "recordedAt"
            ])
            for o in observations:
                rec_at = o.get("recordedAt")
                o_writer.writerow([
                    o.get("userId", ""),
                    o.get("platform", ""),
                    o.get("postId", ""),
                    o.get("url", "") or "",
                    o.get("author", "") or "",
                    o.get("authorDisplayName", "") or "",
                    o.get("authorVerified", "") if o.get("authorVerified") is not None else "",
                    o.get("authorHeadline", "") or "",
                    o.get("isPromoted", "") if o.get("isPromoted") is not None else "",
                    o.get("postedAt", "") or "",
                    o.get("seenAt", "") if o.get("seenAt") is not None else "",
                    o.get("badgeShown", "") if o.get("badgeShown") is not None else "",
                    o.get("dwellMs", 0),
                    o.get("dwellCount", 0),
                    rec_at.isoformat() if hasattr(rec_at, "isoformat") else str(rec_at or ""),
                ])
            zipf.writestr("observations.csv", o_buf.getvalue())

            # ──────────────── 3. Interactions (JSON + CSV) ────────────────
            i_cursor = db.interactions.find()
            interactions = [i async for i in i_cursor]
            manifest["collections"]["interactions"] = len(interactions)
            zipf.writestr("interactions.json", json.dumps(interactions, default=json_serializer, indent=2))

            i_buf = io.StringIO()
            i_writer = csv.writer(i_buf)
            i_writer.writerow(["userId", "platform", "postId", "action", "occurredAt", "recordedAt"])
            for i in interactions:
                rec_at = i.get("recordedAt")
                i_writer.writerow([
                    i.get("userId", ""),
                    i.get("platform", ""),
                    i.get("postId", ""),
                    i.get("action", ""),
                    i.get("occurredAt", "") if i.get("occurredAt") is not None else "",
                    rec_at.isoformat() if hasattr(rec_at, "isoformat") else str(rec_at or ""),
                ])
            zipf.writestr("interactions.csv", i_buf.getvalue())

            # ──────────────── 4. Dwell Logs (JSON + CSV) ────────────────
            dw_cursor = db.dwell_logs.find()
            dwell_logs = [dw async for dw in dw_cursor]
            manifest["collections"]["dwell_logs"] = len(dwell_logs)
            zipf.writestr("dwell_logs.json", json.dumps(dwell_logs, default=json_serializer, indent=2))

            dw_buf = io.StringIO()
            dw_writer = csv.writer(dw_buf)
            dw_writer.writerow(["userId", "platform", "postId", "dwellMs", "isAnomaly", "occurredAt", "recordedAt"])
            for dw in dwell_logs:
                rec_at = dw.get("recordedAt")
                dw_writer.writerow([
                    dw.get("userId", ""),
                    dw.get("platform", ""),
                    dw.get("postId", ""),
                    dw.get("dwellMs", 0),
                    dw.get("isAnomaly", False),
                    dw.get("occurredAt", "") if dw.get("occurredAt") is not None else "",
                    rec_at.isoformat() if hasattr(rec_at, "isoformat") else str(rec_at or ""),
                ])
            zipf.writestr("dwell.csv", dw_buf.getvalue())

            # ──────────────── 5. Scored Posts (JSON) ────────────────
            s_cursor = db.scored_posts.find()
            scored_posts = [s async for s in s_cursor]
            manifest["collections"]["scored_posts"] = len(scored_posts)
            zipf.writestr("scored_posts.json", json.dumps(scored_posts, default=json_serializer, indent=2))

            # ──────────────── 6. Blocked Participants (JSON + CSV) ────────────────
            b_cursor = db.blocked_participants.find()
            blocked_entries = [b async for b in b_cursor]
            manifest["collections"]["blocked_participants"] = len(blocked_entries)
            zipf.writestr("blocked_participants.json", json.dumps(blocked_entries, default=json_serializer, indent=2))

            b_buf = io.StringIO()
            b_writer = csv.writer(b_buf)
            b_writer.writerow(["identifier", "identifierType", "participantId", "externalId", "reason", "blockedAt"])
            for b in blocked_entries:
                b_at = b.get("blockedAt")
                b_writer.writerow([
                    b.get("identifier", ""),
                    b.get("identifierType", ""),
                    b.get("participantId", "") or "",
                    b.get("externalId", "") or "",
                    b.get("reason", "") or "",
                    b_at.isoformat() if hasattr(b_at, "isoformat") else str(b_at or ""),
                ])
            zipf.writestr("blocked_participants.csv", b_buf.getvalue())

            # ──────────────── 7. Manifest ────────────────
            manifest["totalRecords"] = sum(manifest["collections"].values())
            zipf.writestr("manifest.json", json.dumps(manifest, indent=2))

        # Atomically replace destination file with temporary file
        if temp_zip.exists():
            if out_path.exists():
                try:
                    os.replace(temp_zip, out_path)
                except OSError:
                    out_path.unlink(missing_ok=True)
                    temp_zip.rename(out_path)
            else:
                temp_zip.rename(out_path)

        file_size_kb = round(out_path.stat().st_size / 1024, 1)
        logger.info(
            f"[✓] Hourly export replaced successfully -> {out_path} ({file_size_kb} KB, {manifest['totalRecords']} total records)"
        )
        return out_path

    finally:
        if temp_zip.exists():
            try:
                temp_zip.unlink()
            except Exception:
                pass


async def export_database_to_zip(mongo_uri: str, db_name: str, out_path: Path) -> Path:
    """Helper for standalone scripts: connects client, exports, and closes."""
    client = AsyncIOMotorClient(mongo_uri)
    try:
        return await export_database(client[db_name], out_path, db_name=db_name)
    finally:
        client.close()


async def run_daemon_for_db(
    db_getter: Callable[[], Any],
    out_path: Path,
    interval_seconds: int = 3600,
    initial_delay: float = 5.0,
):
    """Automatic background export service running inside the backend server."""
    logger.info(
        f"[Hourly Export] Automatic background export daemon active (interval={interval_seconds}s, out='{out_path.name}')."
    )
    if initial_delay > 0:
        await asyncio.sleep(initial_delay)

    while True:
        try:
            d = db_getter()
            if d is not None:
                await export_database(d, out_path)
            else:
                logger.warning("[Hourly Export] Database not initialized yet; retrying next cycle.")
        except asyncio.CancelledError:
            logger.info("[Hourly Export] Background daemon shutting down cleanly.")
            break
        except Exception as e:
            logger.error(f"[Hourly Export] Error during export: {e}", exc_info=True)

        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            logger.info("[Hourly Export] Sleep interrupted for shutdown.")
            break


async def run_daemon(mongo_uri: str, db_name: str, out_path: Path, interval_seconds: int = 3600):
    """Standalone CLI daemon loop."""
    logger.info(
        f"Starting standalone hourly export daemon (interval={interval_seconds}s, db='{db_name}', target='{out_path}')..."
    )
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: stop_event.set())
        except (NotImplementedError, AttributeError):
            pass

    try:
        while not stop_event.is_set():
            start_ts = time.time()
            try:
                await export_database_to_zip(mongo_uri, db_name, out_path)
            except Exception as e:
                logger.error(f"Error during export: {e}", exc_info=True)

            elapsed = time.time() - start_ts
            sleep_duration = max(1.0, float(interval_seconds) - elapsed)
            logger.info(f"Next export scheduled in {round(sleep_duration)} seconds.")

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=sleep_duration)
            except asyncio.TimeoutError:
                pass
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Hourly export daemon stopped.")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="Hourly Data Save / Export Script for SAIR")
    parser.add_argument(
        "--uri",
        default=os.environ.get("MONGO_URI", "mongodb://localhost:27017"),
        help="MongoDB connection URI (default: from MONGO_URI env or mongodb://localhost:27017)",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("MONGO_DB", "sair"),
        help="MongoDB database name (default: from MONGO_DB env or 'sair')",
    )
    parser.add_argument(
        "--out",
        default="exports.zip",
        help="Target zip archive path (default: 'exports.zip'). File is replaced on each run.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("EXPORT_INTERVAL_SECONDS", "3600")),
        help="Export frequency in seconds for daemon mode (default: 3600 = 1 hour)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run export once immediately and exit (for cron or Windows Task Scheduler)",
    )

    args = parser.parse_args()
    target_path = Path(args.out)

    if args.once:
        asyncio.run(export_database_to_zip(args.uri, args.db, target_path))
    else:
        try:
            asyncio.run(run_daemon(args.uri, args.db, target_path, interval_seconds=args.interval))
        except KeyboardInterrupt:
            logger.info("Exiting on user interrupt.")


if __name__ == "__main__":
    main()
