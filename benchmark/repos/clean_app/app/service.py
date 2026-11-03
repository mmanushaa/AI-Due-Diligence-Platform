"""Application logic.

The service takes an already authenticated :class:`~app.auth.User` and passes its
``owner_id`` down to the store, so scoping is decided once and not per query.
"""

from __future__ import annotations

import sqlite3

from app.auth import User
from app.config import Settings
from app.store import Document, delete_document, get_document, search_documents


class NotFound(Exception):
    """The document does not exist for this owner."""


def list_documents(
    conn: sqlite3.Connection, user: User, term: str, settings: Settings
) -> list[Document]:
    """Return the caller's documents matching ``term``, capped by page size."""
    return search_documents(conn, user.owner_id, term, settings.page_size)


def read_document(conn: sqlite3.Connection, user: User, document_id: int) -> Document:
    """Return one of the caller's documents or raise :class:`NotFound`."""
    document = get_document(conn, user.owner_id, document_id)
    if document is None:
        raise NotFound(f"document {document_id} not found")
    return document


def remove_document(conn: sqlite3.Connection, user: User, document_id: int) -> bool:
    """Delete one of the caller's documents; ``False`` when nothing was removed."""
    return delete_document(conn, user.owner_id, document_id) > 0
