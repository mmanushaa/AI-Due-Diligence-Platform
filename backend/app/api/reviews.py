"""Human review gate endpoints (step 3.4).

A critical audit stops in the workflow's review node. These endpoints are what a
reviewer uses to see the queue and to decide:

    GET  /api/v1/reviews                    the pending reviews
    POST /api/v1/reviews/{id}/decision      approve, override or request evidence

The decision is written here, in one transaction, before the workflow is resumed -
so the reviewer's act is recorded even if the resume fails. The workflow's review
node reads it back rather than trusting a value passed through the graph, which is
what makes "record the decision, then resume" safe to retry.

Nothing is edited in place. An override writes a *new* risk assessment whose
``supersedes_id`` points at the one it disagrees with, and every decision is an
append-only ``review_decisions`` row carrying the original level, the final level
and the reason.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.audit import record
from app.core.deps import require_roles
from app.db.enums import (
    AssessmentStatus,
    AuditStatus,
    ReviewDecision,
    ReviewStatus,
    Severity,
    UserRole,
)
from app.db.models import Audit, AuditLog, Repository, Review, ReviewDecisionRow, RiskAssessment
from app.db.session import get_session

#: Past-tense trail actions: the log states what happened, not what was asked for.
DECISION_ACTION = {
    ReviewDecision.APPROVE: "review_approved",
    ReviewDecision.OVERRIDE: "review_overridden",
    ReviewDecision.REQUEST_EVIDENCE: "review_evidence_requested",
}

router = APIRouter(prefix="/api/v1", tags=["reviews"])


class ReviewOut(BaseModel):
    id: str
    audit_id: str
    assessment_id: str
    status: str
    requested_at: datetime | None
    level: str | None
    repository: str | None


class ReviewList(BaseModel):
    reviews: list[ReviewOut]
    total: int


class DecisionIn(BaseModel):
    decision: Literal["approve", "override", "request_evidence"]
    #: Required for override and request_evidence; a note, not free-form decoration.
    reason: str | None = Field(default=None, max_length=2_000)
    #: Required for override: the level the reviewer believes is right.
    new_level: Literal["critical", "high", "medium", "low", "info"] | None = None


class DecisionOut(BaseModel):
    review_id: str
    review_status: str
    audit_id: str
    audit_status: str
    assessment_id: str | None = None
    superseded_assessment_id: str | None = None
    resumed: bool


def workflow_services():
    """The services the resumed workflow runs with; a test overrides this."""
    from app.workflow.nodes import WorkflowServices

    return WorkflowServices()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _review_out(session: Session, review: Review) -> ReviewOut:
    audit = session.get(Audit, review.audit_id)
    repository = session.get(Repository, audit.repository_id) if audit else None
    assessment = session.get(RiskAssessment, review.assessment_id)
    return ReviewOut(
        id=str(review.id),
        audit_id=str(review.audit_id),
        assessment_id=str(review.assessment_id),
        status=review.status.value,
        requested_at=review.requested_at,
        level=assessment.overall_level.value if assessment else None,
        repository=repository.name if repository else None,
    )


@router.get("/reviews", response_model=ReviewList, summary="List reviews awaiting a decision")
def list_reviews(
    status: str = Query("pending", description="review status to list"),
    user=Depends(require_roles(UserRole.REVIEWER)),
    session: Session = Depends(get_session),
):
    stmt = select(Review).order_by(Review.requested_at.asc(), Review.created_at.asc())
    try:
        stmt = stmt.where(Review.status == ReviewStatus(status))
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"unknown review status {status!r}",
        ) from exc
    rows = list(session.execute(stmt).scalars())
    return ReviewList(
        reviews=[_review_out(session, row) for row in rows], total=len(rows)
    )


@router.post(
    "/reviews/{review_id}/decision",
    response_model=DecisionOut,
    summary="Record a reviewer's decision and resume the audit",
)
def decide(
    review_id: uuid.UUID,
    payload: DecisionIn,
    user=Depends(require_roles(UserRole.REVIEWER)),
    session: Session = Depends(get_session),
):
    review = session.get(Review, review_id)
    if review is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="review not found"
        )
    if review.status is not ReviewStatus.PENDING:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"review is already {review.status.value}",
        )

    reason = " ".join((payload.reason or "").split()) or None
    if payload.decision in (ReviewDecision.OVERRIDE.value, ReviewDecision.REQUEST_EVIDENCE.value):
        if reason is None:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail=f"{payload.decision} requires a reason",
            )
    if payload.decision == ReviewDecision.OVERRIDE.value and payload.new_level is None:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="override requires a new level",
        )

    audit = session.get(Audit, review.audit_id)
    if audit is None:  # pragma: no cover - a review always has an audit
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="audit not found")
    assessment = session.get(RiskAssessment, review.assessment_id)
    original_level = assessment.overall_level if assessment else None

    decision = ReviewDecision(payload.decision)
    superseded: uuid.UUID | None = None
    final_level = original_level

    if decision is ReviewDecision.APPROVE:
        review.status = ReviewStatus.APPROVED
    elif decision is ReviewDecision.OVERRIDE:
        # A new assessment, not an edit: the pipeline's own conclusion stays readable.
        final_level = Severity(payload.new_level)
        superseded = assessment.id if assessment else None
        replacement = RiskAssessment(
            audit_id=audit.id,
            overall_level=final_level,
            overall_score=assessment.overall_score if assessment else None,
            rule_applied="human_override",
            rationale=reason,
            technical_summary={
                "overridden_from": original_level.value if original_level else None,
                "review_id": str(review.id),
            },
            status=AssessmentStatus.FINAL,
            supersedes_id=superseded,
        )
        session.add(replacement)
        review.status = ReviewStatus.OVERRIDDEN
    else:
        review.status = ReviewStatus.NEEDS_EVIDENCE
        audit.status = AuditStatus.NEEDS_EVIDENCE
        record(
            session,
            actor=user.id,
            action="audit_needs_evidence",
            object_type="audit",
            object_id=audit.id,
            before={"status": AuditStatus.AWAITING_REVIEW.value},
            after={"status": AuditStatus.NEEDS_EVIDENCE.value, "reason": reason},
        )

    session.add(
        ReviewDecisionRow(
            review_id=review.id,
            reviewer_id=getattr(user, "id", None),
            decision=decision,
            reason=reason,
            original_level=original_level,
            final_level=final_level,
        )
    )
    record(
        session,
        actor=user.id,
        action=DECISION_ACTION[decision],
        object_type="review",
        object_id=review.id,
        before={
            "status": ReviewStatus.PENDING.value,
            "level": original_level.value if original_level else None,
        },
        after={
            "status": review.status.value,
            "level": final_level.value if final_level else None,
            "reason": reason,
        },
    )
    session.commit()
    session.refresh(review)

    # Recorded, now continue the run. The review node reads the decision back.
    from app.workflow.graph import resume_workflow

    state = resume_workflow(
        audit.id, {"decision": decision.value}, services=workflow_services()
    )
    session.expire_all()
    audit = session.get(Audit, review.audit_id)

    return DecisionOut(
        review_id=str(review.id),
        review_status=review.status.value,
        audit_id=str(audit.id),
        audit_status=audit.status.value,
        assessment_id=str(review.assessment_id),
        superseded_assessment_id=str(superseded) if superseded else None,
        resumed=bool(state),
    )


__all__ = ["router"]
