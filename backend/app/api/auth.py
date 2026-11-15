"""Login, refresh and logout (step 3.6).

There is no signup endpoint. Accounts come from the seed script (``app.core.seed``),
so the set of users is a deployment decision rather than something anyone can create
by calling the API.

The access token is returned in the JSON body and the refresh token only as an
HttpOnly cookie scoped to this router's path, so a script can keep the access token in
memory while a browser never exposes the refresh token to page JavaScript.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import record
from app.core.config import settings
from app.core.auth import (
    ACCESS,
    REFRESH,
    REFRESH_COOKIE,
    REFRESH_COOKIE_PATH,
    TokenError,
    access_token,
    decode_token,
    hash_password,
    refresh_token,
    verify_password,
)
from app.db.models import User
from app.db.session import get_session

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

#: A real bcrypt hash of a value nobody knows, checked when the email does not exist
#: so that an unknown account costs the same time as a wrong password.
DUMMY_HASH = hash_password("no-account-has-this-password")

REFRESH_MAX_AGE = settings.jwt_refresh_days * 24 * 60 * 60


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    id: str
    email: str
    role: str


class LoginOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


def _user_out(user: User) -> UserOut:
    return UserOut(id=str(user.id), email=user.email, role=user.role.value)


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=token,
        max_age=REFRESH_MAX_AGE,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path=REFRESH_COOKIE_PATH,
    )


def _by_email(session: Session, email: str) -> User | None:
    normalised = email.strip().lower()
    return session.execute(
        select(User).where(func.lower(User.email) == normalised)
    ).scalars().first()


@router.post("/login", response_model=LoginOut, summary="Exchange credentials for a token")
def login(
    payload: LoginIn,
    response: Response,
    session: Session = Depends(get_session),
) -> LoginOut:
    user = _by_email(session, payload.email)
    # One message and one code for every failure: which half was wrong is not the
    # caller's business, and a different answer for "no such user" enumerates accounts.
    if user is None:
        verify_password(payload.password, DUMMY_HASH)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not verify_password(payload.password, user.password_hash) or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = access_token(user.id, user.role)
    _set_refresh_cookie(response, refresh_token(user.id, user.role))
    record(
        session,
        actor=user.id,
        action="login",
        object_type="user",
        object_id=user.id,
        after={
            "email": user.email,
            "role": user.role.value,
            "method": "password",
            "at": datetime.now(timezone.utc).isoformat(),
        },
    )
    session.commit()

    return LoginOut(
        access_token=token,
        expires_in=settings.jwt_expire_minutes * 60,
        user=_user_out(user),
    )


@router.post(
    "/refresh",
    response_model=LoginOut,
    summary="Exchange the refresh cookie for a new access token",
)
def refresh(
    response: Response,
    refresh_token_cookie: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
    session: Session = Depends(get_session),
) -> LoginOut:
    if not refresh_token_cookie:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="no refresh token"
        )
    try:
        claims = decode_token(refresh_token_cookie, expect=REFRESH)
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc

    user = session.get(User, claims.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")

    # Rotated on every use: a stolen refresh token is only good until the owner's
    # browser refreshes next, and the role is re-read rather than trusted from the old
    # token, so a demotion takes effect at the next refresh.
    _set_refresh_cookie(response, refresh_token(user.id, user.role))
    return LoginOut(
        access_token=access_token(user.id, user.role),
        expires_in=settings.jwt_expire_minutes * 60,
        user=_user_out(user),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Drop the refresh cookie")
def logout(response: Response) -> Response:
    response.delete_cookie(key=REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


__all__ = ["router", "ACCESS", "LoginIn", "LoginOut"]
