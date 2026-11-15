"""Create the one account per role (step 3.6).

No public signup: the users who can reach the platform are whatever this module was
last run with. Credentials come from the environment (``SEED_<ROLE>_EMAIL`` and
``SEED_<ROLE>_PASSWORD``) so a password never appears in a command line, a shell
history or a commit.

Running it again with the same email rotates that account's password and role rather
than creating a second account: seeding should converge, not accumulate.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Mapping

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.auth import PasswordError, hash_password
from app.db.enums import UserRole

ROLE_ENV: dict[UserRole, tuple[str, str]] = {
    UserRole.ANALYST: ("SEED_ANALYST_EMAIL", "SEED_ANALYST_PASSWORD"),
    UserRole.REVIEWER: ("SEED_REVIEWER_EMAIL", "SEED_REVIEWER_PASSWORD"),
    UserRole.ADMIN: ("SEED_ADMIN_EMAIL", "SEED_ADMIN_PASSWORD"),
}


@dataclass
class SeedReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.invalid


def seed_users(env: Mapping[str, str], session: Session) -> SeedReport:
    """Create or update one user per role from ``env``; never prints a password."""
    # Imported here so the module can be imported without a database.
    from app.db.models import User

    report = SeedReport()
    for role, (email_key, password_key) in ROLE_ENV.items():
        email = (env.get(email_key) or "").strip()
        password = env.get(password_key) or ""
        if not email or not password:
            report.missing.append(role.value)
            continue
        if "@" not in email:
            report.invalid.append(f"{role.value}: {email_key} is not an email address")
            continue
        try:
            password_hash = hash_password(password)
        except PasswordError as exc:
            report.invalid.append(f"{role.value}: {password_key} {exc}")
            continue

        user = session.execute(
            select(User).where(func.lower(User.email) == email.lower())
        ).scalars().first()
        if user is None:
            session.add(
                User(
                    id=uuid.uuid4(),
                    email=email,
                    password_hash=password_hash,
                    role=role,
                    is_active=True,
                )
            )
            report.created.append(f"{role.value} <{email}>")
        else:
            user.role = role
            user.password_hash = password_hash
            user.is_active = True
            report.updated.append(f"{role.value} <{email}>")

    session.commit()
    return report


__all__ = ["seed_users", "SeedReport", "ROLE_ENV"]
