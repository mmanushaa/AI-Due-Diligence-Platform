"""Helpers shared by the scanner adapters.

The masking and fingerprint rules are the same for every scanner: a stored
secret is never plaintext, and a fingerprint is a hash over category, rule,
file and line range only.
"""

from __future__ import annotations

import hashlib
import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Finding

MAX_SNIPPET_CHARS = 500
MAX_ERROR_CHARS = 2000

# Token shapes that must never survive into a log line, a snippet or an error.
TOKEN_PATTERNS = (
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


def mask_secret(value: str) -> str:
    """Return a preview that cannot be used to recover the secret."""
    if not value:
        return "***"
    prefix = value[:4] if len(value) > 8 else ""
    return f"{prefix}***REDACTED***({len(value)} chars)"


def redact_text(text: str, secret: str | None = None) -> str:
    """Remove a known secret and any recognisable token shape from free text."""
    if secret:
        text = text.replace(secret, mask_secret(secret))
    for pattern in TOKEN_PATTERNS:
        text = pattern.sub(lambda match: mask_secret(match.group(0)), text)
    return text


def finding_fingerprint(
    *, category: str, rule_id: str, file_path: str, line_start: int, line_end: int
) -> str:
    """hash(category, rule, file, line range) per the Backend Schema dedupe rule."""
    material = "|".join([category, rule_id, file_path, str(line_start), str(line_end)])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def existing_fingerprints(session: Session, audit_id: uuid.UUID) -> set[str]:
    """Fingerprints already stored for this audit.

    This is the dedupe gate every adapter checks before inserting, so a second run of
    the same audit stores nothing new. The unique (audit_id, fingerprint) constraint on
    `findings` is the second line of defence for concurrent writers.
    """
    return set(
        session.execute(
            select(Finding.fingerprint).where(Finding.audit_id == audit_id)
        ).scalars()
    )
