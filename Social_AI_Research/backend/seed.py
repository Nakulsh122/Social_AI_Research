"""Provision a participant and print their bearer token.

Usage:
    python seed.py <participantId> [role]

    role defaults to "participant"; pass "admin" for researcher accounts that
    can query other participants' observations.

The printed token is NOT retrievable later — copy it the moment it's printed.
"""

import asyncio
import secrets
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

import db


async def main(participant_id: str, role: str) -> None:
    await db.connect()
    token = secrets.token_urlsafe(32)
    try:
        await db.state.db.participants.insert_one({
            "participantId": participant_id,
            "token": token,
            "role": role,
            "active": True,
            "createdAt": datetime.now(timezone.utc),
        })
    finally:
        # Always close so the script exits cleanly.
        pass

    print(f"Created participant '{participant_id}' (role={role})")
    print(f"  token: {token}")
    print()
    print("  Copy this token NOW — it is not retrievable after this point.")
    print("  Use it as `Authorization: Bearer <token>` in extension requests.")
    await db.disconnect()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    pid = sys.argv[1]
    r = sys.argv[2] if len(sys.argv) > 2 else "participant"
    if r not in ("participant", "admin"):
        print(f"role must be 'participant' or 'admin', got {r!r}")
        sys.exit(2)
    asyncio.run(main(pid, r))
