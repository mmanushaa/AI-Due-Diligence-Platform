"""Data access.

Every statement is parameterised and every read is scoped by ``owner_id``, so a
caller can never reach another tenant's row even if it guesses a primary key.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Document:
    """One stored document."""

    document_id: int
    owner_id: int
    title: str
    body: str


def _row_to_document(row: tuple[int, int, str, str]) -> Document:
    return Document(document_id=row[0], owner_id=row[1], title=row[2], body=row[3])


def search_documents(conn: sqlite3.Connection, owner_id: int, term: str, limit: int) -> list[Document]:
    """Return the owner's documents whose title contains ``term``."""
    pattern = f"%{term.replace('%', r'\%').replace('_', r'\_')}%"
    rows = conn.execute(
        "SELECT id, owner_id, title, body FROM documents"
        " WHERE owner_id = ? AND title LIKE ? ESCAPE '\\'"
        " ORDER BY title LIMIT ?",
        (owner_id, pattern, limit),
    ).fetchall()
    return [_row_to_document(row) for row in rows]


def get_document(conn: sqlite3.Connection, owner_id: int, document_id: int) -> Document | None:
    """Return one of the owner's documents, or ``None`` when it is not theirs."""
    row = conn.execute(
        "SELECT id, owner_id, title, body FROM documents WHERE owner_id = ? AND id = ?",
        (owner_id, document_id),
    ).fetchone()
    return None if row is None else _row_to_document(row)


def delete_document(conn: sqlite3.Connection, owner_id: int, document_id: int) -> int:
    """Delete one of the owner's documents; returns the number of rows removed."""
    cursor = conn.execute(
        "DELETE FROM documents WHERE owner_id = ? AND id = ?",
        (owner_id, document_id),
    )
    conn.commit()
    return cursor.rowcount
