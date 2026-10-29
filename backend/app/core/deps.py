"""Temporary authentication stub.

Step 1.2 has no login flow yet, so every request is attributed to one fixed
stub user. A real users row is created on first use so foreign keys such as
`repositories.uploaded_by` resolve.
"""

import uuid

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.enums import UserRole
from app.db.models import User
from app.db.session import get_session

STUB_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
STUB_USER_EMAIL = "stub-analyst@localhost.invalid"
# Not a credential: this account can never be logged into, it only exists to
# satisfy the uploader foreign key.
STUB_PASSWORD_HASH = "stub-account-no-login"


def get_current_user(session: Session = Depends(get_session)) -> User:
    """Return the fixed stub user, creating the row if it is missing.

    TODO(step 3.6): replace with JWT authentication and a real user lookup.
    """
    user = session.get(User, STUB_USER_ID)
    if user is None:
        user = User(
            id=STUB_USER_ID,
            email=STUB_USER_EMAIL,
            password_hash=STUB_PASSWORD_HASH,
            role=UserRole.ANALYST,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
    return user
