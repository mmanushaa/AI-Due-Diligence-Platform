"""Step 3.6: login, tokens and the role matrix.

These tests use the real dependencies (no override from conftest): a token has to be
obtained the way a client obtains one, and each endpoint has to answer 401 or 403 for
the right reason.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.seed import seed_users
from app.db.enums import UserRole
from app.db.models import AuditLog, User
from app.db.session import get_session_factory
from app.main import app

pytestmark = pytest.mark.real_auth

PASSWORDS = {
    UserRole.ANALYST: "analyst-password-1",
    UserRole.REVIEWER: "reviewer-password-1",
    UserRole.ADMIN: "admin-password-1",
}


@pytest.fixture(scope="module")
def accounts():
    """One account per role, created the way the seed script creates them."""
    suffix = uuid.uuid4().hex[:8]
    session = get_session_factory()()
    users = {}
    for role in PASSWORDS:
        user = User(
            id=uuid.uuid4(),
            email=f"{role.value}-{suffix}@example.invalid",
            password_hash="placeholder",
            role=role,
            is_active=True,
        )
        session.add(user)
        users[role] = user
    session.commit()
    for user in users.values():
        session.refresh(user)
    accounts = {
        role: {"id": user.id, "email": user.email, "password": PASSWORDS[role]}
        for role, user in users.items()
    }

    # Hash through the seed path rather than by hand, so these accounts have exactly
    # the hashes a deployment would have.
    seed_users(
        {
            "SEED_ANALYST_EMAIL": accounts[UserRole.ANALYST]["email"],
            "SEED_ANALYST_PASSWORD": PASSWORDS[UserRole.ANALYST],
            "SEED_REVIEWER_EMAIL": accounts[UserRole.REVIEWER]["email"],
            "SEED_REVIEWER_PASSWORD": PASSWORDS[UserRole.REVIEWER],
            "SEED_ADMIN_EMAIL": accounts[UserRole.ADMIN]["email"],
            "SEED_ADMIN_PASSWORD": PASSWORDS[UserRole.ADMIN],
        },
        session,
    )
    session.close()

    yield accounts

    session = get_session_factory()()
    # Deleting a user with decisions on record makes Postgres null the foreign key,
    # which is an UPDATE on an append-only table. Step around the trigger here only.
    session.execute(text("ALTER TABLE review_decisions DISABLE TRIGGER USER"))
    for account in accounts.values():
        session.delete(session.get(User, account["id"]))
    session.commit()
    session.execute(text("ALTER TABLE review_decisions ENABLE TRIGGER USER"))
    session.commit()
    session.close()


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def token_for(client: TestClient, account: dict) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": account["email"], "password": account["password"]},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# login
# --------------------------------------------------------------------------- #


def test_login_returns_an_access_token_and_sets_the_refresh_cookie(client, accounts):
    response = client.post(
        "/api/v1/auth/login",
        json={"email": accounts[UserRole.ANALYST]["email"], "password": PASSWORDS[UserRole.ANALYST]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    assert body["user"]["role"] == "analyst"
    assert body["access_token"].count(".") == 2

    cookie = response.headers["set-cookie"]
    assert "refresh_token=" in cookie
    assert "HttpOnly" in cookie
    assert "Path=/api/v1/auth" in cookie
    # The refresh token itself is never in the body.
    assert "refresh_token" not in body


def test_login_is_written_to_the_audit_trail(client, accounts):
    token_for(client, accounts[UserRole.ADMIN])

    session = get_session_factory()()
    try:
        rows = (
            session.query(AuditLog)
            .filter(
                AuditLog.actor == str(accounts[UserRole.ADMIN]["id"]),
                AuditLog.action == "login",
            )
            .all()
        )
    finally:
        session.close()

    assert rows
    assert rows[-1].object_type == "user"
    assert rows[-1].after["method"] == "password"
    # The trail names the method; it must not carry the credential itself.
    assert "access_token" not in rows[-1].after
    assert "password_hash" not in rows[-1].after


@pytest.mark.parametrize("password", ["wrong-password", ""])
def test_a_wrong_password_is_rejected(client, accounts, password):
    response = client.post(
        "/api/v1/auth/login",
        json={"email": accounts[UserRole.ANALYST]["email"], "password": password},
    )

    assert response.status_code in (401, 422)
    if response.status_code == 401:
        assert response.json()["detail"] == "invalid email or password"


def test_an_unknown_email_gets_the_same_answer_as_a_wrong_password(client, accounts):
    response = client.post(
        "/api/v1/auth/login",
        json={"email": f"nobody-{uuid.uuid4().hex[:6]}@example.invalid", "password": "whatever-123"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid email or password"


def test_a_deactivated_account_cannot_log_in(client, accounts):
    account = accounts[UserRole.ANALYST]
    session = get_session_factory()()
    session.get(User, account["id"]).is_active = False
    session.commit()
    try:
        response = client.post(
            "/api/v1/auth/login",
            json={"email": account["email"], "password": account["password"]},
        )
    finally:
        session.get(User, account["id"]).is_active = True
        session.commit()
        session.close()

    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# 401: no usable token
# --------------------------------------------------------------------------- #


def test_every_protected_endpoint_answers_401_without_a_token(client):
    audit_id = uuid.uuid4()
    for method, path in (
        ("get", "/api/v1/reviews"),
        ("post", f"/api/v1/reviews/{uuid.uuid4()}/decision"),
        ("get", f"/api/v1/audits/{audit_id}"),
        ("get", f"/api/v1/audits/{audit_id}/findings"),
        ("get", f"/api/v1/findings/{uuid.uuid4()}"),
        ("get", f"/api/v1/audits/{audit_id}/report"),
        ("get", f"/api/v1/audits/{audit_id}/audit-log"),
        ("post", f"/api/v1/repositories/{uuid.uuid4()}/audits"),
    ):
        kwargs = {"json": {"decision": "approve"}} if method == "post" else {}
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 401, f"{method.upper()} {path} -> {response.status_code}"
        assert response.headers.get("www-authenticate") == "Bearer"


def test_a_forged_token_is_rejected(client):
    response = client.get(
        "/api/v1/reviews", headers=headers("not.a.jwt")
    )

    assert response.status_code == 401


def test_an_expired_token_is_rejected(client, accounts):
    from datetime import datetime, timedelta, timezone

    import jwt

    from app.core.config import settings

    account = accounts[UserRole.ADMIN]
    expired = jwt.encode(
        {
            "sub": str(account["id"]),
            "role": "admin",
            "kind": "access",
            "jti": uuid.uuid4().hex,
            "iat": datetime.now(timezone.utc) - timedelta(hours=2),
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),
        },
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )

    response = client.get("/api/v1/reviews", headers=headers(expired))

    assert response.status_code == 401


def test_a_refresh_token_cannot_authenticate_a_request(client, accounts):
    from app.core.auth import refresh_token

    account = accounts[UserRole.ADMIN]
    token = refresh_token(account["id"], UserRole.ADMIN)

    response = client.get("/api/v1/reviews", headers=headers(token))

    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# 403: signed in, wrong role
# --------------------------------------------------------------------------- #


def test_an_analyst_cannot_reach_the_review_queue_or_decide(client, accounts):
    token = token_for(client, accounts[UserRole.ANALYST])

    queue = client.get("/api/v1/reviews", headers=headers(token))
    decision = client.post(
        f"/api/v1/reviews/{uuid.uuid4()}/decision",
        json={"decision": "approve"},
        headers=headers(token),
    )

    assert queue.status_code == 403
    assert decision.status_code == 403
    assert "reviewer" in queue.json()["detail"]


def test_an_analyst_cannot_read_the_audit_trail(client, accounts):
    token = token_for(client, accounts[UserRole.ANALYST])

    response = client.get(f"/api/v1/audits/{uuid.uuid4()}/audit-log", headers=headers(token))

    assert response.status_code == 403


def test_a_reviewer_cannot_start_an_audit(client, accounts):
    token = token_for(client, accounts[UserRole.REVIEWER])

    response = client.post(
        f"/api/v1/repositories/{uuid.uuid4()}/audits", headers=headers(token)
    )

    assert response.status_code == 403
    assert "analyst" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# each role reaches what it should
# --------------------------------------------------------------------------- #


def test_an_analyst_reaches_the_analyst_endpoints(client, accounts):
    token = token_for(client, accounts[UserRole.ANALYST])
    audit_id = uuid.uuid4()

    # 404, not 401/403: the role check passed and the audit simply does not exist.
    for path in (
        f"/api/v1/audits/{audit_id}",
        f"/api/v1/audits/{audit_id}/findings",
        f"/api/v1/audits/{audit_id}/report",
    ):
        response = client.get(path, headers=headers(token))
        assert response.status_code == 404, f"{path} -> {response.status_code}"


def test_a_reviewer_reaches_the_review_queue(client, accounts):
    token = token_for(client, accounts[UserRole.REVIEWER])

    response = client.get("/api/v1/reviews", headers=headers(token))

    assert response.status_code == 200
    assert "reviews" in response.json()


def test_an_admin_reaches_both_sides(client, accounts):
    token = token_for(client, accounts[UserRole.ADMIN])

    assert client.get("/api/v1/reviews", headers=headers(token)).status_code == 200
    assert (
        client.get(f"/api/v1/audits/{uuid.uuid4()}/audit-log", headers=headers(token)).status_code
        == 404
    )
    assert (
        client.get(f"/api/v1/audits/{uuid.uuid4()}", headers=headers(token)).status_code == 404
    )


# --------------------------------------------------------------------------- #
# refresh and logout
# --------------------------------------------------------------------------- #


def test_refresh_exchanges_the_cookie_for_a_new_access_token(client, accounts):
    first = client.post(
        "/api/v1/auth/login",
        json={"email": accounts[UserRole.REVIEWER]["email"], "password": PASSWORDS[UserRole.REVIEWER]},
    ).json()["access_token"]

    refreshed = client.post("/api/v1/auth/refresh")

    assert refreshed.status_code == 200
    body = refreshed.json()
    assert body["access_token"] != first
    assert client.get("/api/v1/reviews", headers=headers(body["access_token"])).status_code == 200


def test_refresh_without_a_cookie_is_401(client):
    response = client.post("/api/v1/auth/refresh")

    assert response.status_code == 401


def test_an_access_token_is_not_accepted_as_a_refresh_token(accounts):
    # A fresh client, so the only cookie is the one this test sets.
    with TestClient(app) as unauthenticated:
        token = token_for(unauthenticated, accounts[UserRole.ANALYST])
        unauthenticated.cookies.clear()
        unauthenticated.cookies.set("refresh_token", token, path="/api/v1/auth")

        response = unauthenticated.post("/api/v1/auth/refresh")

    assert response.status_code == 401


def test_logout_drops_the_refresh_cookie(client, accounts):
    token_for(client, accounts[UserRole.ANALYST])

    response = client.post("/api/v1/auth/logout")
    after = client.post("/api/v1/auth/refresh")

    assert response.status_code == 204
    assert after.status_code == 401


# --------------------------------------------------------------------------- #
# seeding
# --------------------------------------------------------------------------- #


def test_the_seed_creates_one_user_per_role_and_converges():
    suffix = uuid.uuid4().hex[:6]
    env = {}
    for role in (UserRole.ANALYST, UserRole.REVIEWER, UserRole.ADMIN):
        env[f"SEED_{role.value.upper()}_EMAIL"] = f"seed-{role.value}-{suffix}@example.invalid"
        env[f"SEED_{role.value.upper()}_PASSWORD"] = f"seed-{role.value}-password"

    session = get_session_factory()()
    try:
        first = seed_users(env, session)
        second = seed_users(env, session)
        rows = (
            session.query(User)
            .filter(User.email.like(f"seed-%-{suffix}@example.invalid"))
            .all()
        )
        roles = sorted(user.role.value for user in rows)
        session.execute(text("ALTER TABLE review_decisions DISABLE TRIGGER USER"))
        for user in rows:
            session.delete(user)
        session.commit()
        session.execute(text("ALTER TABLE review_decisions ENABLE TRIGGER USER"))
        session.commit()
    finally:
        session.close()

    assert first.ok and second.ok
    assert len(first.created) == 3 and not first.updated
    assert len(second.updated) == 3 and not second.created
    assert roles == ["admin", "analyst", "reviewer"]


def test_the_seed_reports_missing_variables_instead_of_guessing():
    session = get_session_factory()()
    try:
        report = seed_users({"SEED_ANALYST_EMAIL": "only-email@example.invalid"}, session)
    finally:
        session.close()

    assert not report.ok
    assert sorted(report.missing) == ["admin", "analyst", "reviewer"]
