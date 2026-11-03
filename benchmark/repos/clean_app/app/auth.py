"""Caller identity.

Every route resolves the caller through :func:`current_user`; there is no code
path that reaches the store without a user object.
"""

from __future__ import annotations

from dataclasses import dataclass

SESSION_COOKIE = "clean_app_session"


class Unauthenticated(Exception):
    """The caller presented no usable session."""


@dataclass(frozen=True)
class User:
    """The authenticated caller and the tenant it belongs to."""

    user_id: int
    owner_id: int
    is_admin: bool = False


def current_user(session: dict[str, object] | None) -> User:
    """Resolve the caller from the session, or refuse the request."""
    if not session:
        raise Unauthenticated("no session")
    user_id = session.get("user_id")
    owner_id = session.get("owner_id")
    if not isinstance(user_id, int) or not isinstance(owner_id, int):
        raise Unauthenticated("incomplete session")
    return User(user_id=user_id, owner_id=owner_id, is_admin=bool(session.get("is_admin")))
