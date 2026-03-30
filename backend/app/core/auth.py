"""
app/core/auth.py
----------------
Authentication and authorisation utilities.

Provides:
  - create_access_token  — signs a JWT with sub, role, exp
  - verify_token         — decodes and validates a JWT
  - hash_password        — bcrypt hash (cost ≥ 12)
  - verify_password      — bcrypt verify
  - get_current_user     — FastAPI dependency: extracts JWT from Bearer header
  - require_role         — FastAPI dependency factory: enforces role allowlist

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings
from app.core.exceptions import Forbidden, Unauthorized

# ── Constants ─────────────────────────────────────────────────────────────────

ALGORITHM = "HS256"
DEFAULT_EXPIRE_HOURS = 8

# ── Password hashing ──────────────────────────────────────────────────────────

# bcrypt with cost factor 12 (Requirement 3.5, 2.5)
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


def hash_password(plain: str) -> str:
    """Return a bcrypt hash of *plain* with cost factor ≥ 12."""
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if *plain* matches *hashed*."""
    return _pwd_context.verify(plain, hashed)


# ── JWT ───────────────────────────────────────────────────────────────────────

def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """
    Sign a JWT containing *data* plus an ``exp`` claim.

    Parameters
    ----------
    data:
        Payload dict — must include ``sub`` and ``role``.
    expires_delta:
        Override the default 8-hour expiry (Requirement 3.1).

    Returns
    -------
    str
        Encoded JWT string.
    """
    payload: dict[str, Any] = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta if expires_delta is not None else timedelta(hours=DEFAULT_EXPIRE_HOURS)
    )
    payload["exp"] = expire
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def verify_token(token: str) -> dict:
    """
    Decode and validate *token*.

    Raises
    ------
    Unauthorized
        If the token is missing, expired, or has an invalid signature.

    Returns
    -------
    dict
        The decoded JWT payload.
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        raise Unauthorized()


# ── FastAPI dependencies ──────────────────────────────────────────────────────

_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> dict:
    """
    FastAPI dependency — extract and verify the JWT from the ``Authorization: Bearer`` header.

    Returns the decoded payload dict (contains ``sub``, ``role``, etc.).
    Raises ``Unauthorized`` (HTTP 401) if the token is absent or invalid.
    """
    if credentials is None:
        raise Unauthorized()
    return verify_token(credentials.credentials)


def require_role(*roles: str):
    """
    FastAPI dependency factory — enforce that the current user's role is in *roles*.

    Usage::

        @router.get("/admin-only")
        async def admin_endpoint(user=Depends(require_role("super_admin"))):
            ...

    Raises
    ------
    Unauthorized
        If no valid JWT is present.
    Forbidden
        If the user's role is not in the allowed *roles* (Requirement 3.4).
    """
    async def _dependency(
        current_user: dict = Depends(get_current_user),
    ) -> dict:
        if current_user.get("role") not in roles:
            raise Forbidden()
        return current_user

    return _dependency
