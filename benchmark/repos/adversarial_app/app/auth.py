"""Caller identity.

This helper is correct. The planted issues live where routes and store functions
fail to use it.
"""

from __future__ import annotations

from dataclasses import dataclass


class Unauthenticated(Exception):
    """No usable session was presented."""


@dataclass(frozen=True)
class User:
    """The authenticated caller and the tenant it belongs to."""

    user_id: int
    owner_id: int


def current_user(session: dict[str, object] | None) -> User:
    """Resolve the caller from the session or refuse the request."""
    if not session:
        raise Unauthenticated("no session")
    user_id = session.get("user_id")
    owner_id = session.get("owner_id")
    if not isinstance(user_id, int) or not isinstance(owner_id, int):
        raise Unauthenticated("incomplete session")
    return User(user_id=user_id, owner_id=owner_id)
