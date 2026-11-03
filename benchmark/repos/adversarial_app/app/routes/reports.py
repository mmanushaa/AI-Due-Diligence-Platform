"""Report routes.

This module is the correct pattern in this repository: the caller is resolved
first and the query is scoped to the caller's tenant.
"""

from __future__ import annotations

import sqlite3

from app.auth import User, current_user


def handle_list_reports(conn: sqlite3.Connection, session: dict[str, object] | None) -> list[dict[str, object]]:
    """List the caller's reports."""
    user: User = current_user(session)
    rows = conn.execute(
        "SELECT id, title FROM reports WHERE owner_id = ?", (user.owner_id,)
    ).fetchall()
    return [{"id": row[0], "title": row[1]} for row in rows]
