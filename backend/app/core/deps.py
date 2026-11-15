"""Authentication and role enforcement (step 3.6).

Every API endpoint takes one of these two dependencies:

    get_current_user          any signed-in user
    require_roles(...)        a signed-in user holding one of the listed roles

``require_roles`` treats ``admin`` as a superset: an admin may call every endpoint.
That is deliberate - an auditor who cannot read the audit trail without a second
account is a support problem, not a security feature - and it is the only implicit
grant in the system.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.auth import ACCESS, TokenError, decode_token
from app.db.enums import UserRole
from app.db.models import User
from app.db.session import get_session

bearer_scheme = HTTPBearer(
    auto_error=False,
    description="Access token from POST /api/v1/auth/login",
)

UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def _user_from_token(token: str | None, session: Session) -> User:
    """Decode a bearer token and load its account."""
    if not token:
        raise UNAUTHENTICATED

    try:
        claims = decode_token(token, expect=ACCESS)
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user = session.get(User, claims.user_id)
    # A token for a deleted or deactivated account stops working immediately: the
    # token is valid, the account behind it is checked every request.
    if user is None or not user.is_active:
        raise UNAUTHENTICATED
    return user


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[Session, Depends(get_session)],
) -> User:
    """The user behind the request's bearer token."""
    if credentials is None:
        raise UNAUTHENTICATED
    return _user_from_token(credentials.credentials, session)


def get_current_user_allow_query(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[Session, Depends(get_session)],
    token: Annotated[str | None, Query(description="access token, for EventSource")] = None,
) -> User:
    """Like :func:`get_current_user`, but also accepts ``?token=``.

    ``EventSource`` cannot set an Authorization header, so the one endpoint that
    streams to it takes the same token in the query string. That is a real trade-off -
    URLs end up in proxy logs - so it is offered by this dependency alone and the
    access token is the short-lived one, not the refresh cookie.
    """
    if credentials is not None and credentials.credentials:
        return _user_from_token(credentials.credentials, session)
    return _user_from_token(token, session)


def require_roles(*roles: UserRole, user_dep=get_current_user):
    """Dependency factory: the caller must hold one of ``roles`` (or be an admin)."""
    allowed = tuple(roles)
    names = ", ".join(role.value for role in allowed)

    # A plain default, not Annotated: this module uses postponed annotations, and an
    # Annotated dependency captured in the closure cannot be resolved from module
    # globals, so FastAPI would silently read the parameter as a query field.
    def dependency(user: User = Depends(user_dep)) -> User:
        if user.role is UserRole.ADMIN or user.role in allowed:
            return user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"this endpoint requires the {names} role",
        )

    dependency.__name__ = f"require_roles({names})"
    return dependency


__all__ = [
    "get_current_user",
    "get_current_user_allow_query",
    "require_roles",
    "bearer_scheme",
]
