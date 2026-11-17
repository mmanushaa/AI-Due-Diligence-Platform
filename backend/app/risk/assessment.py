"""The overall risk level for an audit (steps 3.3 and 3.4).

The arithmetic lives in :mod:`app.risk.scoring`: a severity floor, a short list of
named escalation rules that can only raise the level, and an aggregate score that is a
sum rather than an average. This module is the database half - it reads the findings,
stores the result and keeps the per-finding factors that produced it, so a reviewer can
reconstruct the score by hand.

Assessments are append-only. A human override does not edit the row it disagrees
with: it writes a new row whose ``supersedes_id`` points at the old one, so the
report can show both what the pipeline concluded and what a reviewer decided.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.enums import AssessmentStatus, Severity
from app.db.models import Finding, RiskAssessment, RiskFactor
from app.risk.scoring import EXCLUDED, SEVERITY_WEIGHT, FindingInput, Score, evaluate

#: Kept for callers that only care about the base rule; the stored ``rule_applied``
#: names any escalation that fired as well (``severity_floor+aggregate_score``).
RULE = "severity_floor"


def current_assessment(session: Session, audit_id: uuid.UUID) -> RiskAssessment | None:
    """The newest assessment for an audit (a human override supersedes the old one)."""
    return session.execute(
        select(RiskAssessment)
        .where(RiskAssessment.audit_id == audit_id)
        .order_by(RiskAssessment.created_at.desc(), RiskAssessment.id.desc())
        .limit(1)
    ).scalars().first()


def _inputs(findings: list[Finding]) -> list[FindingInput]:
    return [
        FindingInput(
            severity=row.severity,
            confidence=row.confidence,
            status=row.verification_status,
            category=row.category,
            title=row.title,
            evidence_paths=tuple(
                evidence.file_path for evidence in row.evidence if evidence.file_path
            ),
        )
        for row in findings
    ]


def score_findings(findings: list[Finding]) -> Score:
    """Pure scoring for a list of rows; the DB write happens in :func:`assess`."""
    return evaluate(_inputs(findings))


def _counted(findings: list[Finding]) -> list[Finding]:
    return [row for row in findings if row.verification_status not in EXCLUDED]


def _factor_rows(
    assessment_id: uuid.UUID, findings: list[Finding], score: Score
) -> list[RiskFactor]:
    counted = _counted(findings)
    rows: list[RiskFactor] = []
    for finding, item in zip(counted, score.per_finding):
        rows.append(
            RiskFactor(
                assessment_id=assessment_id,
                finding_id=finding.id,
                severity_weight=item.severity_weight,
                evidence_confidence=item.evidence_confidence,
                exposure_multiplier=item.exposure_multiplier,
                finding_score=item.score,
                notes=item.notes,
            )
        )
    return rows


def assess(session: Session, audit_id: uuid.UUID) -> RiskAssessment:
    """Compute this audit's risk level from its findings and store it."""
    findings = list(
        session.execute(
            select(Finding)
            .where(Finding.audit_id == audit_id)
            .options(selectinload(Finding.evidence))
        ).scalars().unique()
    )
    score = score_findings(findings)
    previous = current_assessment(session, audit_id)

    assessment = RiskAssessment(
        audit_id=audit_id,
        overall_level=score.level,
        overall_score=score.score,
        rule_applied=score.rule_applied,
        rationale=score.rationale,
        technical_summary={
            "findings": len(findings),
            "counted": sum(score.counts.values()),
            "rejected": len(findings) - sum(score.counts.values()),
            "counts": score.counts,
            "highest": score.floor.value,
            "score": str(score.score),
            "escalations": list(score.rules),
            "severity_weights": {
                level.value: str(weight) for level, weight in SEVERITY_WEIGHT.items()
            },
            "factors": [
                {
                    "finding_id": str(finding.id),
                    "severity_weight": str(item.severity_weight),
                    "evidence_confidence": str(item.evidence_confidence),
                    "exposure_multiplier": str(item.exposure_multiplier),
                    "score": str(item.score),
                    "hints": list(item.hints),
                }
                for finding, item in zip(_counted(findings), score.per_finding)
            ],
        },
        status=(
            AssessmentStatus.PENDING_REVIEW
            if score.level is Severity.CRITICAL
            else AssessmentStatus.FINAL
        ),
        supersedes_id=previous.id if previous is not None else None,
    )
    session.add(assessment)
    session.flush()
    session.add_all(_factor_rows(assessment.id, findings, score))
    session.commit()
    session.refresh(assessment)
    return assessment
