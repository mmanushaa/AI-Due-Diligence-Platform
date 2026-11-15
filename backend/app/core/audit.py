"""The audit trail service (step 3.5).

Every state change in the product writes one row here: who did it, what they did,
which object it happened to, and what the object looked like before and after. The
row is written in the caller's transaction, so a change and its record commit or roll
back together - an audit trail that can silently miss the change it describes is
worse than none.

``audit_log`` is append-only in the database too (migration 0006 installs triggers
that raise on UPDATE and DELETE). This module therefore only ever INSERTs: no update
or delete helper exists, and none should be added.
"""

from __future__ import annotations

import uuid
from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.db.models import AuditLog

MAX_FIELD_CHARS = 2_000


def _clean(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """A JSON-safe copy, with values that are too long truncated rather than dropped."""
    if value is None:
        return None
    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        key = str(key)[:100]
        if isinstance(item, (uuid.UUID,)):
            cleaned[key] = str(item)
        elif isinstance(item, (str, int, float, bool)) or item is None:
            cleaned[key] = item[:MAX_FIELD_CHARS] if isinstance(item, str) else item
        else:
            cleaned[key] = str(item)[:MAX_FIELD_CHARS]
    return cleaned


def record(
    session: Session,
    *,
    actor: str | uuid.UUID | None,
    action: str,
    object_type: str,
    object_id: str | uuid.UUID,
    before: Mapping[str, Any] | None = None,
    after: Mapping[str, Any] | None = None,
) -> AuditLog:
    """Append one audit-trail row to the caller's transaction.

    ``actor`` is a user id, an agent name or ``"system"``; anything else is a bug in
    the call site, not something to paper over here.
    """
    row = AuditLog(
        actor=str(actor or "system")[:100],
        action=str(action)[:100],
        object_type=str(object_type)[:60],
        object_id=str(object_id),
        before=_clean(before),
        after=_clean(after),
    )
    session.add(row)
    return row


__all__ = ["record"]
