import uuid
import html as _html
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, desc, select
from sqlalchemy.orm import Session, selectinload

from app.api.audits import _to_out
from app.db.enums import AuditStatus, UserRole
from app.db.models import Audit, Evidence, Finding, Repository
from app.db.session import get_session
from app.schemas.findings import (
    AuditOut,
    EvidenceOut,
    FindingDetail,
    FindingEntry,
    FindingList,
    FindingSummary,
    RepositoryOut,
)
from app.core.deps import require_roles

router = APIRouter(prefix="/api/v1", tags=["findings"])

SEVERITY_ORDER = (
    "critical",
    "high",
    "medium",
    "low",
    "info",
)


def _primary_evidence(finding: Finding):
    """The evidence row that locates a finding in the list view.

    Findings carry no file or line of their own; that lives on evidence rows.
    The earliest file:line is the representative location, ordered so the
    choice is stable across requests.
    """
    rows = list(finding.evidence or [])
    if not rows:
        return None
    rows.sort(key=lambda ev: (ev.file_path, ev.line_start, ev.line_end, str(ev.id)))
    return rows[0]


def _severity_rank(severity: str) -> int:
    """0 for critical, 4 for info; unknown values sort last."""
    return SEVERITY_ORDER.index(severity) if severity in SEVERITY_ORDER else len(SEVERITY_ORDER)


def _highest_severity(grouped: dict[str, dict[str, list[Finding]]]) -> str:
    for sev in SEVERITY_ORDER:
        if any(sev in grouped[category] for category in grouped):
            return sev
    return "none"


@router.get(
    "/audits/{audit_id:uuid}/findings",
    response_model=FindingList,
    summary="List findings for an audit with optional filters",
)
def list_findings(
    audit_id: uuid.UUID,
    category: str | None = Query(None, description="Category to filter by"),
    severity: str | None = Query(None, description="Severity to filter by"),
    status: str | None = Query(None, description="Verification status to filter by"),
    file: str | None = Query(None, description="file path substring filter"),
    user=Depends(require_roles(UserRole.ANALYST, UserRole.REVIEWER)),
    session: Session = Depends(get_session),
):
    audit = session.get(Audit, audit_id)
    if not audit:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="audit not found")

    stmt = (
        select(Finding)
        .where(Finding.audit_id == audit_id)
        .options(selectinload(Finding.evidence))
        .order_by(desc(Finding.created_at))
    )
    if category:
        stmt = stmt.where(Finding.category == category)
    if severity:
        stmt = stmt.where(Finding.severity == severity)
    if status:
        stmt = stmt.where(Finding.verification_status == status)
    if file:
        # The path lives on the evidence rows, so filter through the relationship.
        stmt = stmt.where(Finding.evidence.any(Evidence.file_path.icontains(file)))

    findings = session.execute(stmt).scalars().unique().all()
    repository = session.get(Repository, audit.repository_id) if audit.repository_id else None

    grouped: dict[str, dict[str, list[Finding]]] = defaultdict(lambda: defaultdict(list))
    for finding in findings:
        grouped[finding.category.value][finding.severity.value].append(finding)

    by_category: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    high_critical = 0
    for finding in findings:
        by_category[finding.category.value] = by_category.get(finding.category.value, 0) + 1
        by_severity[finding.severity.value] = by_severity.get(finding.severity.value, 0) + 1
        if finding.severity.value in ("high", "critical"):
            high_critical += 1

    return FindingList(
        audit=AuditOut(
            id=str(audit.id),
            status=audit.status.value,
            current_stage=audit.current_stage.value if audit.current_stage else None,
            started_at=audit.started_at,
            finished_at=audit.finished_at,
            error=getattr(audit, "error", None),
        ),
        repository=(
            RepositoryOut(
                id=str(repository.id),
                name=repository.name,
                source_type=repository.source_type,
                source_ref=repository.source_ref,
                storage_path=repository.storage_path,
                sha256=repository.sha256,
                size_bytes=repository.size_bytes,
                created_at=repository.created_at,
            )
            if repository
            else None
        ),
        summary=FindingSummary(
            total=len(findings),
            by_category=by_category,
            by_severity=by_severity,
            high_critical=high_critical,
        ),
        findings=[
            FindingEntry(
                id=str(f.id),
                title=f.title,
                category=f.category.value,
                severity=f.severity.value,
                status=f.verification_status.value,
                confidence=float(f.confidence) if f.confidence is not None else None,
                file_path=(_primary_evidence(f).file_path if _primary_evidence(f) else None),
                line_start=(_primary_evidence(f).line_start if _primary_evidence(f) else None),
                line_end=(_primary_evidence(f).line_end if _primary_evidence(f) else None),
                source_tool=f.source_tool,
                fingerprint=f.fingerprint,
            )
            for f in sorted(
                findings,
                key=lambda f: (
                    _severity_rank(f.severity.value),
                    f.created_at.isoformat() if f.created_at else "",
                ),
            )
        ],
        grouped={
            category: {sev: [str(f.id) for f in fxs] for sev, fxs in sevmap.items()}
            for category, sevmap in grouped.items()
        },
    )


@router.get(
    "/findings/{finding_id:uuid}",
    response_model=FindingDetail,
    summary="Get a single finding with evidence",
)
def get_finding(
    finding_id: uuid.UUID,
    user=Depends(require_roles(UserRole.ANALYST, UserRole.REVIEWER)),
    session: Session = Depends(get_session),
):
    stmt = (
        select(Finding)
        .where(Finding.id == finding_id)
        .options(selectinload(Finding.evidence))
    )
    finding = session.execute(stmt).scalars().first()
    if not finding:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="finding not found")

    audit = session.get(Audit, finding.audit_id)
    repository_id = getattr(audit, "repository_id", None)

    return FindingDetail(
        id=str(finding.id),
        title=finding.title,
        # The schema stores the analyst-facing explanation as `reasoning`.
        description=finding.reasoning,
        category=finding.category.value,
        severity=finding.severity.value,
        status=finding.verification_status.value,
        file_path=(_primary_evidence(finding).file_path if _primary_evidence(finding) else None),
        line_start=(_primary_evidence(finding).line_start if _primary_evidence(finding) else None),
        line_end=(_primary_evidence(finding).line_end if _primary_evidence(finding) else None),
        source_tool=finding.source_tool,
        scanner_tool=getattr(finding, "scanner_tool", None),
        fingerprint=finding.fingerprint,
        confidence=float(finding.confidence) if finding.confidence is not None else None,
        business_impact=finding.business_impact,
        recommendation=finding.recommendation,
        verdict=finding.verdict.value if finding.verdict else None,
        evidence=[
            EvidenceOut(
                id=str(ev.id),
                kind=ev.kind.value,
                file_path=ev.file_path,
                line_start=ev.line_start,
                line_end=ev.line_end,
                snippet=ev.snippet,
                # Evidence rows carry a kind, not a label (see the schema).
                label=None,
            )
            for ev in (finding.evidence or [])
        ],
        audit=AuditOut(
            id=str(audit.id) if audit else "",
            status=audit.status.value if audit else "",
            current_stage=audit.current_stage.value if audit and audit.current_stage else None,
            started_at=audit.started_at if audit else None,
            finished_at=audit.finished_at if audit else None,
            error=getattr(audit, "error", None) if audit else None,
        )
        if audit
        else None,
        repository_id=str(repository_id) if repository_id else None,
    )
