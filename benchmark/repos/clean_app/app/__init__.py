"""A small, deliberately clean document service."""

from app.config import Settings, load_settings
from app.service import NotFound, list_documents, read_document, remove_document

__all__ = [
    "NotFound",
    "Settings",
    "list_documents",
    "load_settings",
    "read_document",
    "remove_document",
]
