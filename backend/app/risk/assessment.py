"""The overall risk level for an audit (step 3.4).

One deterministic rule, so a reviewer can always reconstruct how a level was
reached: the audit's level is the highest severity among its findings that no
validation pass rejected. A single critical finding makes the audit critical; that
is the floor rule, and it is stored on the row as ``rule_applied``.

Assessments are append-only. A human override does not edit the row it disagrees
with: it writes a new row whose ``supersedes_id`` points at the old one, so the
report can show both what the pipeline concluded and what a reviewer decided.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.enums import AssessmentStatus, Severity, VerificationStatus
from app.db.models import Finding, RiskAssessment

RULE = "severity_floor"
SEVERITY_ORDER = (
    Severity.CRITICAL,
    Severity.HIGH,
    Severity.MEDIUM,
    Severity.LOW,
    Severity.INFO,
)
#: A finding the validator rejected is not risk: it is a claim that failed.
EXCLUDED = (VerificationStatus.REJECTED_FALSE_POSITIVE,)


def highest_severity(levels: list[Severity]) -> Severity:
    """The worst level in the list; ``info`` when it is empty."""
    for candidate in SEVERITY_ORDER:
        if candidate in levels:
            return candidate
    return Severity.INFO


def current_assessment(session: Session, audit_id: uuid.UUID) -> RiskAssessment | None:
    """The newest assessment for an audit (a human override supersedes the old one)."""
    return session.execute(
        select(RiskAssessment)
        .where(RiskAssessment.audit_id == audit_id)
        .order_by(RiskAssessment.created_at.desc(), RiskAssessment.id.desc())
        .limit(1)
    ).scalars().first()


def assess(session: Session, audit_id: uuid.UUID) -> RiskAssessment:
    """Compute and store this audit's risk level."""
    findings = list(
        session.execute(select(Finding).where(Finding.audit_id == audit_id)).scalars()
    )
    live = [row for row in findings if row.verification_status not in EXCLUDED]
    level = highest_severity([row.severity for row in live])
    previous = current_assessment(session, audit_id)

    assessment = RiskAssessment(
        audit_id=audit_id,
        overall_level=level,
        overall_score=None,
        rule_applied=RULE,
        rationale=(
            f"{len(live)} finding(s) counted, {len(findings) - len(live)} rejected by"
            f" validation; the highest counted severity is {level.value}, so the level"
            f" is {level.value}."
        ),
        technical_summary={
            "findings": len(findings),
            "counted": len(live),
            "highest": level.value,
        },
        status=(
            AssessmentStatus.PENDING_REVIEW
            if level is Severity.CRITICAL
            else AssessmentStatus.FINAL
        ),
        supersedes_id=previous.id if previous is not None else None,
    )
    session.add(assessment)
    session.commit()
    session.refresh(assessment)
    return assessment
