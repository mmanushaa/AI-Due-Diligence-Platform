"""What every agent stage shares: the index it reads and the rows it writes.

Two stages now produce findings (the single-agent baseline and the security crew),
and both need the same three things: an index that retrieval can actually search,
a fingerprint that stops the same problem being stored twice, and evidence rows
that point at real lines. Keeping that here means a finding from a crew and a
finding from the baseline are stored identically, so the harness compares agents
rather than storage paths.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.schema import AgentFinding
from app.agents.tools import RepositoryTools
from app.db.enums import (
    Category,
    EvidenceKind,
    FindingOrigin,
    Severity,
    Verdict,
    VerificationStatus,
)
from app.db.models import Chunk, Evidence, File, Finding
from app.indexing.service import index_audit, index_graph
from app.intake.filtering import index_snapshot
from app.retrieval.embeddings import embed_chunks
from app.scanners.common import (
    MAX_SNIPPET_CHARS,
    existing_fingerprints,
    finding_fingerprint,
    redact_text,
)

logger = logging.getLogger(__name__)


def ensure_index(session: Session, audit_id: uuid.UUID, tools: RepositoryTools) -> bool:
    """Build the symbol/chunk/vector index the retrieval tools need, once.

    The audit pipeline indexes files for the scanners but stops there; symbols,
    chunks, call edges and vectors are separate stages. Retrieval without them
    returns nothing at all, which would look like an agent that found no problems.
    Building them here keeps a stage correct for any audit that has passed intake,
    and the work is skipped when the index already exists.
    """
    already = session.execute(
        select(Chunk.id).where(Chunk.audit_id == audit_id).limit(1)
    ).first()
    if already is not None:
        return True
    root = tools.snapshot_root
    if root is None or not Path(root).is_dir():
        return False

    index_snapshot(session, audit_id, root)
    index_audit(session, audit_id, snapshot_root=root)
    index_graph(session, audit_id, snapshot_root=root)
    embed_chunks(session, audit_id)
    return True


def file_ids(session: Session, audit_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """Path to ``files.id``, so evidence is joined rather than string-matched."""
    return {
        path: file_id
        for file_id, path in session.execute(
            select(File.id, File.path).where(File.audit_id == audit_id)
        ).all()
    }


def store_findings(
    session: Session,
    audit_id: uuid.UUID,
    findings: tuple[AgentFinding, ...],
    tools: RepositoryTools,
    *,
    source_tool: str,
) -> int:
    """Insert validated findings as AI suggestions; returns how many were new.

    ``origin=agent`` with ``verification_status=ai_suggested`` is the whole point:
    nothing an agent says is presented as verified, and the review workflow can
    find these rows later and confirm or reject them.
    """
    existing = existing_fingerprints(session, audit_id)
    ids = file_ids(session, audit_id)
    stored = 0
    for finding in findings:
        fingerprint = finding_fingerprint(
            category=finding.category,
            rule_id=source_tool,
            file_path=finding.file,
            line_start=finding.line_start,
            line_end=finding.line_end,
        )
        if fingerprint in existing:
            continue

        lines = tools.file_lines(finding.file) or []
        quote = finding.evidence_quote or "\n".join(
            lines[finding.line_start - 1 : finding.line_end]
        )
        row = Finding(
            audit_id=audit_id,
            category=Category(finding.category),
            title=finding.title,
            severity=Severity(finding.severity),
            confidence=Decimal(str(round(finding.confidence, 2))),
            origin=FindingOrigin.AGENT,
            source_tool=source_tool,
            verification_status=VerificationStatus.AI_SUGGESTED,
            verdict=Verdict.ISSUE,
            reasoning=finding.reasoning,
            recommendation=finding.recommendation,
            fingerprint=fingerprint,
        )
        session.add(row)
        session.flush()

        session.add(
            Evidence(
                finding_id=row.id,
                file_id=ids.get(finding.file),
                file_path=finding.file,
                line_start=finding.line_start,
                line_end=finding.line_end,
                snippet=redact_text(quote)[:MAX_SNIPPET_CHARS],
                kind=EvidenceKind.PRIMARY,
            )
        )
        existing.add(fingerprint)
        stored += 1

    session.commit()
    return stored
