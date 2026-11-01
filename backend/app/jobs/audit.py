"""One audit run: intake validation, file filtering, then the scanners (step 1.9).

The runner keeps `audits.status` and `audits.current_stage` honest at every stage, so
the progress screen can poll and a stuck run is visible as `running` on the stage it
reached rather than as silently completed. Any failure sets `failed` and records the
reason in the append-only audit log; the exception is re-raised so the worker reports
the task as failed too.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from app.db.enums import AuditStatus, ScannerTool, Stage
from app.db.models import Audit, AuditLog, Repository
from app.db.session import get_session_factory
from app.intake.filtering import ScanSummary, index_snapshot
from app.scanners.orchestrator import (
    OrchestrationResult,
    ScannerConfigs,
    ScanFunction,
    run_scanners,
)

logger = logging.getLogger(__name__)

MAX_REASON_CHARS = 2000


class AuditRunError(RuntimeError):
    """The audit cannot run: a missing audit, repository or snapshot."""


@dataclass(frozen=True)
class AuditRunResult:
    audit_id: uuid.UUID
    status: AuditStatus
    stage: Stage | None
    files: ScanSummary
    scanners: OrchestrationResult


def validate_snapshot(snapshot_root: Path) -> Path:
    """Intake validation: the stored snapshot must exist and hold at least one file."""
    snapshot = Path(snapshot_root)
    if not snapshot.exists():
        raise AuditRunError(f"snapshot {snapshot} does not exist")
    if not snapshot.is_dir():
        raise AuditRunError(f"snapshot {snapshot} is not a directory")
    if not any(snapshot.rglob("*")):
        raise AuditRunError(f"snapshot {snapshot} is empty")
    return snapshot


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _enter_stage(session: Session, audit: Audit, stage: Stage) -> None:
    """Move the audit to a stage and make that visible before the work starts."""
    audit.current_stage = stage
    session.commit()


def _record_failure(session: Session, audit_id: uuid.UUID, reason: str) -> None:
    """Mark the audit failed and keep the reason in the append-only audit log."""
    audit = session.get(Audit, audit_id)
    if audit is None:  # pragma: no cover - the audit existed when the run started
        return
    audit.status = AuditStatus.FAILED
    audit.finished_at = _now()
    session.add(
        AuditLog(
            actor="system",
            action="audit_failed",
            object_type="audit",
            object_id=str(audit_id),
            before={"status": AuditStatus.RUNNING.value, "stage": _stage_value(audit)},
            after={"reason": reason[:MAX_REASON_CHARS]},
        )
    )
    session.commit()


def _stage_value(audit: Audit) -> str | None:
    return audit.current_stage.value if audit.current_stage is not None else None


def run_audit(
    audit_id: uuid.UUID,
    *,
    session_factory: sessionmaker[Session] | None = None,
    configs: ScannerConfigs | None = None,
    scanners: Mapping[ScannerTool, ScanFunction] | None = None,
    output_root: Path | str | None = None,
) -> AuditRunResult:
    """Run every stage of one audit, updating status and stage as it goes."""
    factory = session_factory or get_session_factory()
    session = factory()
    try:
        audit = session.get(Audit, audit_id)
        if audit is None:
            raise AuditRunError(f"audit {audit_id} does not exist")
        repository = session.get(Repository, audit.repository_id)
        if repository is None:
            raise AuditRunError(f"repository for audit {audit_id} does not exist")

        audit.status = AuditStatus.RUNNING
        audit.started_at = audit.started_at or _now()
        audit.finished_at = None
        _enter_stage(session, audit, Stage.INTAKE)
        logger.info("audit %s entered stage intake", audit_id)

        snapshot = validate_snapshot(Path(repository.storage_path))

        _enter_stage(session, audit, Stage.INDEX)
        logger.info("audit %s entered stage index", audit_id)
        files = index_snapshot(session, audit.id, snapshot)

        _enter_stage(session, audit, Stage.SCANNERS)
        logger.info("audit %s entered stage scanners", audit_id)
        scanners_result = run_scanners(
            audit.id,
            snapshot,
            configs=configs,
            scanners=scanners,
            output_root=output_root,
            session_factory=factory,
        )

        audit = session.get(Audit, audit_id)
        audit.status = AuditStatus.COMPLETED
        audit.finished_at = _now()
        session.commit()
        logger.info(
            "audit %s completed: %d files, %d scanner failure(s)",
            audit_id,
            files.included,
            len(scanners_result.failed),
        )
        return AuditRunResult(
            audit_id=audit_id,
            status=audit.status,
            stage=audit.current_stage,
            files=files,
            scanners=scanners_result,
        )
    except Exception as exc:
        session.rollback()
        reason = f"{type(exc).__name__}: {exc}"
        logger.exception("audit %s failed: %s", audit_id, reason)
        _record_failure(session, audit_id, reason)
        raise
    finally:
        session.close()
