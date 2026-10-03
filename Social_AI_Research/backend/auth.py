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
    return doc
