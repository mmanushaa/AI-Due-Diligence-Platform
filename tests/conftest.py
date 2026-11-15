"""Shared test setup.

Since step 3.6 every API endpoint needs a bearer token. Most tests are about the
pipeline or the database rather than about authentication, so this file signs them in
as one admin account: ``admin`` is a superset of every role, so the override satisfies
any endpoint's role check. Tests that exercise authentication itself mark themselves
``@pytest.mark.real_auth`` and get the real dependencies, no override.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.deps import get_current_user
from app.core.auth import hash_password
from app.db.enums import UserRole
from app.db.models import User
from app.db.session import get_session_factory
from app.main import app as fastapi_app

TEST_PASSWORD = "pytest-suite-password"


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "real_auth: use the real authentication dependencies instead of the sign-in above"
    )


@pytest.fixture(scope="session")
def api_user():
    """One admin account for the whole run, so logins do not dominate the runtime."""
    session = get_session_factory()()
    user = User(
        id=uuid.uuid4(),
        email=f"pytest-suite-{uuid.uuid4().hex[:8]}@example.invalid",
        password_hash=hash_password(TEST_PASSWORD),
        role=UserRole.ADMIN,
        is_active=True,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    yield user
    # A user referenced by review_decisions cannot be deleted without an UPDATE to an
    # append-only table; step around the trigger for this cleanup only.
    from sqlalchemy import text

    session.execute(text("ALTER TABLE review_decisions DISABLE TRIGGER USER"))
    session.delete(session.get(User, user.id))
    session.commit()
    session.execute(text("ALTER TABLE review_decisions ENABLE TRIGGER USER"))
    session.commit()
    session.close()


@pytest.fixture(autouse=True)
def signed_in(request, api_user):
    if request.node.get_closest_marker("real_auth") is not None:
        yield
        return
    fastapi_app.dependency_overrides[get_current_user] = lambda: api_user
    try:
        yield
    finally:
        fastapi_app.dependency_overrides.pop(get_current_user, None)
