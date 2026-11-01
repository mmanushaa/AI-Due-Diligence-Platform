"""Audit API (step 1.9): start an audit and read its progress."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.db.enums import AuditStatus
from app.db.models import Audit, AuditLog, Repository, User
from app.db.session import get_session
from app.jobs.tasks import run_audit_task

router = APIRouter(prefix="/api/v1", tags=["audits"])


class AuditOut(BaseModel):
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


def _to_out(session: Session, audit: Audit) -> AuditOut:
    return AuditOut(
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
    response_model=AuditOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_audit(
    repository_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> AuditOut:
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
    session.commit()
    session.refresh(audit)

    try:
        run_audit_task.delay(str(audit.id))
    except Exception as exc:  # broker down: the audit must not look queued forever
        audit.status = AuditStatus.FAILED
        audit.finished_at = datetime.now(timezone.utc)
        session.add(
            AuditLog(
                actor="system",
                action="audit_failed",
                object_type="audit",
                object_id=str(audit.id),
                before={"status": AuditStatus.QUEUED.value},
                after={"reason": f"could not enqueue the audit: {type(exc).__name__}"},
            )
        )
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="audit queue is unavailable; the audit was not started",
        ) from exc

    return _to_out(session, audit)


@router.get("/audits/{audit_id}", response_model=AuditOut)
def read_audit(
    audit_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> AuditOut:
    """Return the audit's status, current stage and failure reason, if any."""
    audit = session.get(Audit, audit_id)
    if audit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="audit not found")
    return _to_out(session, audit)
