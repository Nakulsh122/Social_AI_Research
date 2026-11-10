from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

import db


security = HTTPBearer(auto_error=True)


async def current_participant(
    creds: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    token = creds.credentials if creds else None
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
        )
    doc = await db.state.db.participants.find_one({"token": token, "active": True})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or inactive participant token",
        )
    if doc.get("blocked"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Participant is blocked",
        )
    # Check if participant identifier is in persistent blocklist
    ext_id = doc.get("externalId")
    pid = doc.get("participantId")
    mongo_id = str(doc.get("_id", ""))
    check_ids = [i for i in [ext_id, pid, mongo_id] if i]
    if check_ids:
        is_blocked = await db.state.db.blocked_participants.find_one({"identifier": {"$in": check_ids}})
        if is_blocked:
            # Sync marked status to participant doc
            await db.state.db.participants.update_one(
                {"_id": doc["_id"]},
                {"$set": {"blocked": True, "blockedReason": is_blocked.get("reason", "Blocklist entry")}}
            )
            doc["blocked"] = True
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Participant is blocked",
            )
    return doc
