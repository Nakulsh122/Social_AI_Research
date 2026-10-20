"""Database backup utility for Social AI Research (SAIR).

Dumps all collections from MongoDB into JSON files and creates a zipped archive.
Can be run locally or on the server.

Usage:
    python backup_db.py
    python backup_db.py --uri mongodb://localhost:27017 --db sair
"""

import argparse
import asyncio
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bson import ObjectId
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv()


def json_serializer(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    return str(obj)


async def backup_database(mongo_uri: str, db_name: str, out_dir: Path) -> Path:
    client = AsyncIOMotorClient(mongo_uri)
    db = client[db_name]

    now_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_folder = out_dir / f"sair_backup_{now_str}"
    backup_folder.mkdir(parents=True, exist_ok=True)

    collections = ["participants", "observations", "interactions", "dwell_logs", "scored_posts"]
    manifest = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "database": db_name,
        "collections": {},
    }

    print(f"[*] Starting backup of database '{db_name}' from {mongo_uri}...")

    for coll_name in collections:
        coll = db[coll_name]
        docs = []
        async for doc in coll.find():
            docs.append(doc)
        
        file_path = backup_folder / f"{coll_name}.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(docs, f, default=json_serializer, indent=2)
        
        manifest["collections"][coll_name] = len(docs)
        print(f"  -> {coll_name}: {len(docs)} documents exported to {file_path.name}")

    # Write manifest
    with open(backup_folder / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    # Create zip archive
    zip_path = out_dir / f"sair_backup_{now_str}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for root, _, files in os.walk(backup_folder):
            for file in files:
                abs_file = Path(root) / file
                rel_path = abs_file.relative_to(backup_folder)
                zipf.write(abs_file, arcname=rel_path)

    print(f"[✓] Backup completed successfully!")
    print(f"    Folder: {backup_folder.resolve()}")
    print(f"    ZIP Archive: {zip_path.resolve()}")
    client.close()
    return zip_path


def main():
    parser = argparse.ArgumentParser(description="Backup SAIR MongoDB Database")
    parser.add_argument("--uri", default=os.environ.get("MONGO_URI", "mongodb://localhost:27017"), help="MongoDB URI")
    parser.add_argument("--db", default=os.environ.get("MONGO_DB", "sair"), help="Database name")
    parser.add_argument("--out", default="backups", help="Output directory")
    args = parser.parse_args()

    out_path = Path(args.out)
    asyncio.run(backup_database(args.uri, args.db, out_path))


if __name__ == "__main__":
    main()

