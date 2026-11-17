"""Audit API (step 1.9): start an audit and read its progress."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.core.audit import record
from app.core.deps import require_roles
from app.db.enums import AuditStatus, UserRole
from app.db.models import (
    Audit,
    AuditLog,
    Finding,
    Report,
    Repository,
    Review,
    RiskAssessment,
    User,
)
from app.db.session import get_session
from app.jobs.tasks import run_audit_task
from app.progress import publish_progress

router = APIRouter(prefix="/api/v1", tags=["audits"])


class AuditResponse(BaseModel):
    id: uuid.UUID
    repository_id: uuid.UUID
    status: str
    current_stage: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None


def _failure_reason(session: Session, audit_id: uuid.UUID) -> str | None:
    """The last recorded failure for this audit, if any."""
    entry = session.execute(
        select(AuditLog.after)
        .where(
            AuditLog.object_type == "audit",
            AuditLog.object_id == str(audit_id),
            AuditLog.action == "audit_failed",
        )
        .order_by(AuditLog.created_at.desc())
        .limit(1)
    ).scalar()
    return (entry or {}).get("reason") if isinstance(entry, dict) else None


def _to_out(session: Session, audit: Audit) -> AuditResponse:
    return AuditResponse(
        id=audit.id,
        repository_id=audit.repository_id,
        status=audit.status.value,
        current_stage=audit.current_stage.value if audit.current_stage else None,
        created_at=audit.created_at,
        started_at=audit.started_at,
        finished_at=audit.finished_at,
        error=_failure_reason(session, audit.id),
    )


@router.post(
    "/repositories/{repository_id}/audits",
    response_model=AuditResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_audit(
    repository_id: uuid.UUID,
    user: User = Depends(require_roles(UserRole.ANALYST)),
    session: Session = Depends(get_session),
) -> AuditResponse:
    """Queue an audit for an uploaded repository and hand it to the worker."""
    repository = session.get(Repository, repository_id)
    if repository is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="repository not found")

    audit = Audit(
        repository_id=repository.id,
        status=AuditStatus.QUEUED,
        started_by=user.id,
    )
    session.add(audit)
    session.flush()
    record(
        session,
        actor=user.id,
        action="audit_created",
        object_type="audit",
        object_id=audit.id,
        after={
            "repository_id": str(repository.id),
            "status": audit.status.value,
            "repository": repository.name,
        },
    )
    session.commit()
    session.refresh(audit)
    # A tracker opened right after this call should see every stage as pending rather
    # than wait for the worker's first event.
    publish_progress(session, audit.id)

    try:
        run_audit_task.delay(str(audit.id))
    except Exception as exc:  # broker down: the audit must not look queued forever
        audit.status = AuditStatus.FAILED
        audit.finished_at = datetime.now(timezone.utc)
        record(
            session,
            actor="system",
            action="audit_failed",
            object_type="audit",
            object_id=audit.id,
            before={"status": AuditStatus.QUEUED.value},
            after={"reason": f"could not enqueue the audit: {type(exc).__name__}"},
        )
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="audit queue is unavailable; the audit was not started",
        ) from exc

    return _to_out(session, audit)


@router.get("/audits/{audit_id}", response_model=AuditResponse)
def read_audit(
    audit_id: uuid.UUID,
    user: User = Depends(require_roles(UserRole.ANALYST, UserRole.REVIEWER)),
    session: Session = Depends(get_session),
) -> AuditResponse:
    """Return the audit's status, current stage and failure reason, if any."""
    audit = session.get(Audit, audit_id)
    if audit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="audit not found")
    return _to_out(session, audit)


class AuditLogEntry(BaseModel):
    """One append-only row of the audit trail."""

    id: str
    actor: str
    action: str
    object_type: str
    object_id: str
    before: dict | None
    after: dict | None
    created_at: datetime


class AuditLogOut(BaseModel):
    audit_id: str
    entries: list[AuditLogEntry]
    total: int


def _related_object_ids(session: Session, audit_id: uuid.UUID) -> set[str]:
    """Every id whose audit-trail rows belong to this audit.

    ``audit_log`` records an object, not an audit: a finding's row carries the
    finding's id. The audit's timeline is therefore the audit's own rows plus the
    rows of everything the audit owns.
    """
    ids: set[str] = {str(audit_id)}
    for model in (Finding, Review, RiskAssessment, Report):
        rows = session.execute(
            select(model.id).where(model.audit_id == audit_id)
        ).scalars()
        ids.update(str(value) for value in rows)
    return ids


@router.get(
    "/audits/{audit_id}/audit-log",
    response_model=AuditLogOut,
    summary="The append-only audit trail for one audit",
)
def read_audit_log(
    audit_id: uuid.UUID,
    user: User = Depends(require_roles(UserRole.REVIEWER, UserRole.ADMIN)),
    session: Session = Depends(get_session),
) -> AuditLogOut:
    audit = session.get(Audit, audit_id)
    if audit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="audit not found")

    ids = _related_object_ids(session, audit_id)
    rows = list(
        session.execute(
            select(AuditLog)
            .where(
                or_(
                    AuditLog.object_id.in_(ids),
                    and_(
                        AuditLog.object_type == "audit",
                        AuditLog.object_id == str(audit_id),
                    ),
                )
            )
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        ).scalars()
    )
    return AuditLogOut(
        audit_id=str(audit_id),
        total=len(rows),
        entries=[
            AuditLogEntry(
                id=str(row.id),
                actor=row.actor,
                action=row.action,
                object_type=row.object_type,
                object_id=row.object_id,
                before=row.before,
                after=row.after,
                created_at=row.created_at,
            )
            for row in rows
        ],
    )
