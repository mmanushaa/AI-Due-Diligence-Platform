"""Seed one user per role from the environment.

    SEED_ANALYST_EMAIL=analyst@example.com  SEED_ANALYST_PASSWORD=... \
    SEED_REVIEWER_EMAIL=reviewer@example.com SEED_REVIEWER_PASSWORD=... \
    SEED_ADMIN_EMAIL=admin@example.com       SEED_ADMIN_PASSWORD=... \
    backend/.venv/Scripts/python.exe backend/scripts/seed_users.py

Exits 2 and changes nothing if any of the six variables is missing or unusable.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.seed import ROLE_ENV, seed_users  # noqa: E402
from app.db.session import get_session_factory  # noqa: E402


def main() -> int:
    session = get_session_factory()()
    try:
        report = seed_users(os.environ, session)
    finally:
        session.close()

    for line in report.created:
        print(f"created {line}")
    for line in report.updated:
        print(f"updated {line}")
    if report.missing:
        print(
            "missing environment variables for: "
            + ", ".join(report.missing)
            + " (each role needs its own SEED_<ROLE>_EMAIL and SEED_<ROLE>_PASSWORD)",
            file=sys.stderr,
        )
    for problem in report.invalid:
        print(f"invalid: {problem}", file=sys.stderr)
    if not report.ok:
        return 2

    print(f"{len(ROLE_ENV)} accounts ready: " + ", ".join(role.value for role in ROLE_ENV))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
