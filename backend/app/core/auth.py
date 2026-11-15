"""Passwords and tokens (step 3.6).

Passwords are hashed with bcrypt (a deliberately slow, salted hash). Access tokens are
short-lived JWTs the client sends as a bearer token; refresh tokens are longer-lived
JWTs the browser holds in an HttpOnly cookie, so page JavaScript never sees them.

Both token kinds carry a ``kind`` claim and are only accepted where that kind belongs:
a refresh token cannot authenticate a request, and an access token cannot mint a new
session. There is no server-side session table; a token is valid until it expires, so
keep the access lifetime short and the signing secret private.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.core.config import settings
from app.db.enums import UserRole

ACCESS = "access"
REFRESH = "refresh"
REFRESH_COOKIE = "refresh_token"
REFRESH_COOKIE_PATH = "/api/v1/auth"

#: bcrypt hashes at most 72 bytes and would silently ignore the rest, so a longer
#: password is refused rather than quietly weakened.
MAX_PASSWORD_BYTES = 72
MIN_PASSWORD_BYTES = 8


class PasswordError(ValueError):
    """The password cannot be hashed as given."""


class TokenError(Exception):
    """The token is missing, malformed, of the wrong kind, or expired."""


def hash_password(password: str) -> str:
    raw = password.encode("utf-8")
    if len(raw) < MIN_PASSWORD_BYTES:
        raise PasswordError(f"a password needs at least {MIN_PASSWORD_BYTES} bytes")
    if len(raw) > MAX_PASSWORD_BYTES:
        raise PasswordError(f"a password may be at most {MAX_PASSWORD_BYTES} bytes")
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    """True only for the password that produced the hash; never raises."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


def _secret() -> str:
    return settings.jwt_secret.get_secret_value()


def _mint(user_id: uuid.UUID, role: UserRole, kind: str, lifetime: timedelta) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(user_id),
            "role": role.value,
            "kind": kind,
            "jti": uuid.uuid4().hex,
            "iat": now,
            "exp": now + lifetime,
        },
        _secret(),
        algorithm=settings.jwt_algorithm,
    )


def access_token(user_id: uuid.UUID, role: UserRole) -> str:
    return _mint(
        user_id, role, ACCESS, timedelta(minutes=settings.jwt_expire_minutes)
    )


def refresh_token(user_id: uuid.UUID, role: UserRole) -> str:
    return _mint(user_id, role, REFRESH, timedelta(days=settings.jwt_refresh_days))


@dataclass(frozen=True)
class TokenClaims:
    user_id: uuid.UUID
    role: UserRole
    kind: str
    expires_at: datetime


def decode_token(token: str, *, expect: str) -> TokenClaims:
    """Verify a token and return its claims, or raise :class:`TokenError`."""
    try:
        payload = jwt.decode(
            token,
            _secret(),
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub", "kind"]},
        )
    except jwt.PyJWTError as exc:  # bad signature, expired, not a JWT at all
        raise TokenError("invalid or expired token") from exc

    if payload.get("kind") != expect:
        raise TokenError(f"expected a {expect} token")
    try:
        return TokenClaims(
            user_id=uuid.UUID(payload["sub"]),
            role=UserRole(payload.get("role", UserRole.ANALYST.value)),
            kind=expect,
            expires_at=datetime.fromtimestamp(payload["exp"], timezone.utc),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise TokenError("malformed token claims") from exc
