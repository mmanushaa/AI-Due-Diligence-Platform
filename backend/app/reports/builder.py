"""Build the report document from stored rows (step 3.9).

Everything this module reads is a record of something that happened: findings and their
evidence, the current risk assessment, review decisions, scanner failures and the audit
itself. It never re-runs a scanner, never asks a model, and never reads the workflow's
in-memory state - so a report is reproducible from the database alone, which is what
makes it auditable.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.enums import VerificationStatus
from app.db.models import (
    Audit,
    AuditLog,
    Finding,
    Repository,
    Review,
    ReviewDecisionRow,
    ScannerFailure,
    User,
)
from app.reports.document import (
    AuditView,
    CategoryRow,
    FindingView,
    ReportDocument,
    ReviewView,
    RiskView,
)
from app.reports.reporter import SEVERITY_RANK, validate_citations, write_summary
from app.risk.assessment import current_assessment

ACTIONABLE_STATUSES = {
    VerificationStatus.SCANNER_CONFIRMED.value,
    VerificationStatus.AI_SUGGESTED.value,
    VerificationStatus.VERIFIED.value,
}
INSUFFICIENT = VerificationStatus.UNRESOLVED.value


def _finding_view(finding: Finding) -> FindingView:
    evidence = list(finding.evidence or [])
    primary = next((item for item in evidence if item.file_path), None)
    return FindingView(
        id=str(finding.id),
        title=finding.title,
        category=finding.category.value,
        severity=finding.severity.value,
        status=finding.verification_status.value,
        verdict=finding.verdict.value if finding.verdict else None,
        confidence=float(finding.confidence) if finding.confidence is not None else None,
        file_path=primary.file_path if primary else None,
        line_start=primary.line_start if primary else None,
        line_end=primary.line_end if primary else None,
        recommendation=finding.recommendation,
        evidence_lines=len(evidence),
    )


def _categories(findings: list[FindingView]) -> list[CategoryRow]:
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for finding in findings:
        counts[finding.category][finding.severity] += 1
    rows = [
        CategoryRow(category=category, counts=dict(by_severity), total=sum(by_severity.values()))
        for category, by_severity in counts.items()
    ]
    return sorted(rows, key=lambda row: (-row.total, row.category))


def _priorities(findings: list[FindingView]) -> list[FindingView]:
    """Most urgent first: verified before unverified, then severity, then confidence."""
    actionable = [
        f for f in findings if f.status != VerificationStatus.REJECTED_FALSE_POSITIVE.value
    ]
    return sorted(
        actionable,
        key=lambda f: (
            f.status in ACTIONABLE_STATUSES,
            SEVERITY_RANK.get(f.severity, 0),
            f.confidence or 0,
        ),
        reverse=True,
    )


def _limitations(
    session: Session,
    audit: Audit,
    findings: list[FindingView],
) -> list[str]:
    lines: list[str] = []

    unresolved = [f for f in findings if f.status == INSUFFICIENT]
    if unresolved:
        lines.append(
            f"{len(unresolved)} finding(s) are listed as insufficient evidence and were not "
            "confirmed against the code: " + ", ".join(f.id for f in unresolved)
        )
    insufficient_verdict = [
        f for f in findings if f.verdict == "insufficient_evidence" and f.status != INSUFFICIENT
    ]
    if insufficient_verdict:
        lines.append(
            f"{len(insufficient_verdict)} finding(s) were assessed as insufficient evidence: "
            + ", ".join(f.id for f in insufficient_verdict)
        )

    failures = session.execute(
        select(ScannerFailure)
        .where(ScannerFailure.audit_id == audit.id)
        .order_by(ScannerFailure.created_at)
    ).scalars().all()
    for failure in failures:
        lines.append(f"Scanner {failure.tool} did not complete: {failure.reason}")

    if audit.status.value in {"needs_evidence", "failed"}:
        reason = session.execute(
            select(AuditLog)
            .where(
                AuditLog.object_type == "audit",
                AuditLog.object_id == str(audit.id),
                AuditLog.action.in_(["audit_incomplete", "audit_failed"]),
            )
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        ).scalars().first()
        detail = (reason.after or {}).get("reason") if reason else None
        lines.append(
            f"The audit closed as {audit.status.value}"
            + (f": {detail}" if detail else "")
            + ". Findings reflect the stages that completed."
        )

    if not findings:
        lines.append("No findings were recorded for this audit.")
    return lines


def _review(session: Session, audit_id: uuid.UUID) -> ReviewView | None:
    review = session.execute(
        select(Review).where(Review.audit_id == audit_id).order_by(Review.created_at.desc())
    ).scalars().first()
    if review is None:
        return None
    decision = session.execute(
        select(ReviewDecisionRow)
        .where(ReviewDecisionRow.review_id == review.id)
        .order_by(ReviewDecisionRow.created_at.desc())
    ).scalars().first()
    reviewer = session.get(User, decision.reviewer_id) if decision and decision.reviewer_id else None
    return ReviewView(
        decision=review.status.value,
        reason=decision.reason if decision else None,
        original_level=(
            decision.original_level.value if decision and decision.original_level else None
        ),
        final_level=decision.final_level.value if decision and decision.final_level else None,
        reviewer=reviewer.email if reviewer else None,
        decided_at=(decision.created_at if decision else review.created_at),
    )


def build_report(session: Session, audit_id: uuid.UUID) -> ReportDocument:
    """Assemble the document; raises ``LookupError`` when the audit does not exist."""
    audit = session.get(Audit, audit_id)
    if audit is None:
        raise LookupError(f"audit {audit_id} does not exist")

    repository = session.get(Repository, audit.repository_id) if audit.repository_id else None
    findings = [
        _finding_view(finding)
        for finding in session.execute(
            select(Finding)
            .where(Finding.audit_id == audit_id)
            .options(selectinload(Finding.evidence))
            .order_by(Finding.created_at)
        ).scalars().unique()
    ]
    assessment = current_assessment(session, audit_id)

    claims = write_summary(findings)
    known_ids = {finding.id for finding in findings}
    kept, dropped = validate_citations(claims, known_ids)

    top = [
        finding
        for finding in sorted(
            (
                f
                for f in findings
                if f.status != VerificationStatus.REJECTED_FALSE_POSITIVE.value
            ),
            key=lambda f: (SEVERITY_RANK.get(f.severity, 0), f.confidence or 0),
            reverse=True,
        )
    ][:5]

    counts: dict[str, int] = defaultdict(int)
    for finding in findings:
        counts[finding.severity] += 1

    return ReportDocument(
        audit=AuditView(
            audit_id=str(audit.id),
            repository=repository.name if repository else "(deleted repository)",
            status=audit.status.value,
            started_at=audit.started_at,
            finished_at=audit.finished_at,
            spent_usd=str(audit.cost_usd),
            budget_usd=str(audit.budget_usd) if audit.budget_usd is not None else None,
        ),
        risk=(
            RiskView(
                level=assessment.overall_level.value,
                rule=assessment.rule_applied,
                rationale=assessment.rationale,
                score=float(assessment.overall_score) if assessment.overall_score is not None else None,
                supersedes_id=str(assessment.supersedes_id) if assessment.supersedes_id else None,
            )
            if assessment
            else None
        ),
        claims=kept,
        top_findings=top,
        categories=_categories(findings),
        priorities=_priorities(findings),
        limitations=_limitations(session, audit, findings),
        review=_review(session, audit_id),
        totals=dict(counts),
        citations_dropped=dropped,
        generated_at=datetime.now(timezone.utc),
    )


__all__ = ["build_report"]
