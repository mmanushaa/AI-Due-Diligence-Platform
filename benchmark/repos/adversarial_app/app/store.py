"""Data access for reports and documents."""

from __future__ import annotations

import sqlite3


def search_documents(conn: sqlite3.Connection, term: str) -> list[tuple[int, str]]:
    """Return documents whose title matches ``term``."""
    statement = "SELECT id, title FROM documents WHERE title LIKE '%%%s%%'" % term
    return conn.execute(statement).fetchall()


def export_report(conn: sqlite3.Connection, report_id: int) -> tuple[int, int, str] | None:
    """Return one report row by id."""
    return conn.execute(
        "SELECT id, owner_id, body FROM reports WHERE id = ?", (report_id,)
    ).fetchone()


def delete_document(conn: sqlite3.Connection, document_id: int) -> int:
    """Delete a document row by id."""
    cursor = conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
    conn.commit()
    return cursor.rowcount
