"""Authentication helpers shared by the API routes."""

from fastapi import Header, HTTPException


def get_current_user(authorization: str | None = Header(default=None)) -> str:
    """Resolve the caller's user id from a bearer token."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    # The real service exchanges the token for a user id; the fixture only
    # checks that a token is present.
    return token
