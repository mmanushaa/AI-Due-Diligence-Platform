"""Document routes."""

from __future__ import annotations

import sqlite3

from app import store
from app.auth import current_user


def handle_read_document(
    conn: sqlite3.Connection, session: dict[str, object] | None, document_id: int
) -> dict[str, object]:
    """Read one document belonging to the caller."""
    user = current_user(session)
    row = conn.execute(
        "SELECT id, title FROM documents WHERE id = ? AND owner_id = ?",
        (document_id, user.owner_id),
    ).fetchone()
    return {"found": row is not None}


def handle_delete_document(
    conn: sqlite3.Connection, session: dict[str, object] | None, document_id: int
) -> dict[str, object]:
    """Delete a document."""
    current_user(session)
    return {"deleted": store.delete_document(conn, document_id) > 0}
