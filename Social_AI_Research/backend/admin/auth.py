"""Admin-side auth — single shared password (ADMIN_SECRET env var).

Cookie value is HMAC-SHA256 of the secret, so the raw secret never appears in
HTTP cookies or server access logs. On every request we re-derive the expected
value from the env var and constant-time compare.
"""

import hashlib
import hmac
import os

from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse


COOKIE_NAME = "sair_admin"
# Bumping this prefix invalidates all outstanding sessions even if the secret
# itself doesn't change (useful if you want to forcibly log everyone out).
_TOKEN_PREFIX = "admin_v1:"


def admin_secret() -> str:
    s = os.environ.get("ADMIN_SECRET", "").strip()
    if not s:
        raise RuntimeError(
            "ADMIN_SECRET is not set. Add it to backend/.env before starting the server."
        )
    return s


def expected_cookie_value() -> str:
    secret = admin_secret()
    return hashlib.sha256((_TOKEN_PREFIX + secret).encode("utf-8")).hexdigest()


def verify_password(submitted: str) -> bool:
    """Constant-time compare of submitted password against ADMIN_SECRET."""
    return hmac.compare_digest(submitted or "", admin_secret())


def verify_cookie(value: str | None) -> bool:
    if not value:
        return False
    return hmac.compare_digest(value, expected_cookie_value())


def is_authenticated(request: Request) -> bool:
    return verify_cookie(request.cookies.get(COOKIE_NAME))


async def require_admin(request: Request) -> None:
    """FastAPI dependency: 401 if no valid admin cookie."""
    if not is_authenticated(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin login required",
        )


def require_admin_or_redirect(request: Request) -> RedirectResponse | None:
    """For HTML routes — returns a redirect to /admin/login if not authed,
    or None if the request is authenticated. Use as:

        redirect = require_admin_or_redirect(request)
        if redirect: return redirect
    """
    if is_authenticated(request):
        return None
    return RedirectResponse(url="/admin/login", status_code=302)
