"""HTTP handlers.

Each handler resolves the caller first and only then touches the service, so an
unauthenticated request never reaches a query.
"""

from __future__ import annotations

import sqlite3

from app.auth import User, current_user
from app.config import Settings
from app.service import NotFound, list_documents, read_document, remove_document


def handle_search(
    conn: sqlite3.Connection, session: dict[str, object] | None, term: str, settings: Settings
) -> list[dict[str, object]]:
    """Search the caller's documents."""
    user: User = current_user(session)
    documents = list_documents(conn, user, term, settings)
    return [{"id": d.document_id, "title": d.title} for d in documents]


def handle_read(
    conn: sqlite3.Connection, session: dict[str, object] | None, document_id: int
) -> dict[str, object]:
    """Read one document belonging to the caller."""
    user: User = current_user(session)
    try:
        document = read_document(conn, user, document_id)
    except NotFound:
        return {"error": "not found"}
    return {"id": document.document_id, "title": document.title, "body": document.body}


def handle_delete(
    conn: sqlite3.Connection, session: dict[str, object] | None, document_id: int
) -> dict[str, object]:
    """Delete one document belonging to the caller."""
    user: User = current_user(session)
    return {"deleted": remove_document(conn, user, document_id)}
