"""HTTP handlers for the report service."""

from app.routes.documents import handle_delete_document, handle_read_document
from app.routes.export import handle_export
from app.routes.reports import handle_list_reports

__all__ = [
    "handle_delete_document",
    "handle_export",
    "handle_list_reports",
    "handle_read_document",
]
