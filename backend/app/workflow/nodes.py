"""The workflow's nodes (step 3.2).

Each node does one stage and returns the state fields it changed; LangGraph owns
the order, the checkpoints between them and the resume. The services a node calls
are injected (:class:`WorkflowServices`) so a test can drive the graph without
docker images or a model, and so the same node code runs in production unchanged.

Failure paths, from the TRD:

* a scanner that fails is recorded in ``scanner_failures`` and the run continues -
  the report states the gap instead of the audit dying;
* a model failure is retried by the policy in :mod:`app.workflow.retry`, then by the
  client's own fallback, and whatever is still unproven is marked ``unresolved``;
* ``BudgetExceeded`` stops the run cleanly: the stages that already ran keep their
  results, and the audit is labelled incomplete rather than failed.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langgraph.types import interrupt
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.agents.multi import run_multi_agent
from app.core.config import settings as app_settings
from app.db.enums import (
    AuditStatus,
    FindingOrigin,
    ReviewStatus,
    Severity,
    Stage,
    VerificationStatus,
)
from app.core.audit import record
from app.db.models import (
    Audit,
    AuditLog,
    Finding,
    FindingEvent,
    Repository,
    Review,
    ScannerFailure,
)
from app.risk.assessment import assess
from app.db.session import get_session_factory
from app.intake.filtering import index_snapshot
from app.observability.tracing import get_tracer
from app.progress import publish_progress
from app.jobs.audit import validate_snapshot
from app.llm.client import BudgetExceeded, LLMError
from app.scanners.orchestrator import run_scanners
from app.validation.validator import validate_audit
from app.workflow.retry import run_with_retry
from app.workflow.state import AuditState

logger = logging.getLogger(__name__)

MAX_REASON_CHARS = 2_000
UNRESOLVED_NOTE = "model failure: the claim could not be checked against the code"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- services ---------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowServices:
    """The stages' implementations, injectable so tests need no docker or model."""

    #: A sessionmaker (or any zero-argument callable that returns a Session).
    session_factory: Callable[[], Session] = get_session_factory()
    index: Callable[[Session, uuid.UUID, Path], Any] = index_snapshot
    scanners: Callable[[uuid.UUID, Path], Any] = run_scanners
    agents: Callable[[Session, uuid.UUID], Any] = run_multi_agent
    validation: Callable[..., Any] = validate_audit
    assess: Callable[[Session, uuid.UUID], Any] = assess


def _stages(state: AuditState, name: str) -> list[str]:
    return [*state.completed, name]


def _load(session: Session, audit_id: str) -> tuple[Audit, Repository]:
    audit = session.get(Audit, uuid.UUID(audit_id))
    if audit is None:
        raise ValueError(f"audit {audit_id} does not exist")
    repository = session.get(Repository, audit.repository_id)
    if repository is None:
        raise ValueError(f"repository for audit {audit_id} does not exist")
    return audit, repository


def _set_stage(
    session: Session,
    audit: Audit,
    stage: Stage,
    status: AuditStatus = AuditStatus.RUNNING,
) -> None:
    """Move the audit to a stage and record the transition in the audit trail."""
    before = {
        "stage": audit.current_stage.value if audit.current_stage else None,
        "status": audit.status.value,
    }
    audit.status = status
    audit.current_stage = stage
    record(
        session,
        actor="system",
        action="stage_changed",
        object_type="audit",
        object_id=audit.id,
        before=before,
        after={"stage": stage.value, "status": status.value},
    )


def _commit_and_announce(session: Session, audit: Audit) -> None:
    """Commit the stage change, then tell the SSE subscribers about it.

    After the commit on purpose: an event describes state that is already durable, so
    a tracker that reconnects does not see a stage that a rolled-back transaction took
    away. A Redis failure is logged inside :func:`publish_progress` and never fails the
    stage.
    """
    session.commit()
    publish_progress(session, audit.id)


def _failure_reason(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:MAX_REASON_CHARS]


def mark_unresolved(session: Session, audit_id: uuid.UUID, reason: str) -> int:
    """Mark every unvalidated agent finding unresolved; returns how many changed.

    The TRD's last step of the model-failure path. A finding nobody could check is
    not a false positive and not verified: it is ``unresolved``, which is what the
    report counts as needing evidence.
    """
    rows = session.execute(
        select(Finding).where(
            Finding.audit_id == audit_id,
            Finding.origin == FindingOrigin.AGENT,
            Finding.verification_status == VerificationStatus.AI_SUGGESTED,
        )
    ).scalars()
    changed = 0
    for finding in rows:
        session.add(
            FindingEvent(
                finding_id=finding.id,
                from_status=finding.verification_status,
                to_status=VerificationStatus.UNRESOLVED,
                actor="workflow",
                note=reason[:500],
            )
        )
        record(
            session,
            actor="workflow",
            action="finding_status_changed",
            object_type="finding",
            object_id=finding.id,
            before={"status": finding.verification_status.value},
            after={"status": VerificationStatus.UNRESOLVED.value, "reason": reason[:400]},
        )
        finding.verification_status = VerificationStatus.UNRESOLVED
        changed += 1
    session.commit()
    return changed


def _is_budget_failure(text: str) -> bool:
    return "budget" in text.lower()


# --- nodes ------------------------------------------------------------------


def intake(state: AuditState, services: WorkflowServices | None = None) -> dict[str, Any]:
    """Validate the snapshot and put the audit into the run."""
    services = services or WorkflowServices()

    def work() -> dict[str, Any]:
        session = services.session_factory()
        try:
            audit, repository = _load(session, state.audit_id)
            snapshot = validate_snapshot(Path(repository.storage_path or ""))
            _set_stage(session, audit, Stage.INTAKE)
            audit.started_at = audit.started_at or _now()
            audit.finished_at = None
            audit.workflow_thread_id = audit.workflow_thread_id or state.audit_id
            _commit_and_announce(session, audit)
            # One trace per audit, opened where the audit's run actually begins; a
            # resumed run reopens the same id rather than starting a second trace.
            get_tracer().start_audit(
                audit_id=audit.id,
                repository=repository.name,
                started_by=str(audit.started_by) if audit.started_by else None,
                budget_usd=audit.budget_usd,
            )
            return {
                "status": "running",
                "stage": "intake",
                "repository_id": str(repository.id),
                "snapshot": str(snapshot),
                "completed": _stages(state, "intake"),
            }
        finally:
            session.close()

    return run_with_retry("intake", work)


def index(state: AuditState, services: WorkflowServices | None = None) -> dict[str, Any]:
    """Index the snapshot's files and record how many were included."""
    services = services or WorkflowServices()

    def work() -> dict[str, Any]:
        session = services.session_factory()
        try:
            audit, _ = _load(session, state.audit_id)
            _set_stage(session, audit, Stage.INDEX)
            _commit_and_announce(session, audit)
            summary = services.index(session, uuid.UUID(state.audit_id), Path(state.snapshot or ""))
            return {
                "stage": "index",
                "files_included": int(getattr(summary, "included", 0) or 0),
                "files_excluded": int(getattr(summary, "excluded", 0) or 0),
                "completed": _stages(state, "index"),
            }
        finally:
            session.close()

    return run_with_retry("index", work)


def scanners(state: AuditState, services: WorkflowServices | None = None) -> dict[str, Any]:
    """Run every scanner; a failure is recorded and the run continues."""
    services = services or WorkflowServices()

    def work() -> dict[str, Any]:
        session = services.session_factory()
        try:
            audit, _ = _load(session, state.audit_id)
            _set_stage(session, audit, Stage.SCANNERS)
            _commit_and_announce(session, audit)
        finally:
            session.close()

        result = services.scanners(
            uuid.UUID(state.audit_id),
            Path(state.snapshot or ""),
            session_factory=services.session_factory,
        )
        failures = {
            outcome.tool.value: (outcome.error or outcome.status.value)[:400]
            for outcome in result.failed
        }
        if failures:
            # Step 3.9: the report has to name the scanners that did not run, and it is
            # generated later from the database, not from this run's memory.
            session = services.session_factory()
            try:
                audit_uuid = uuid.UUID(state.audit_id)
                recorded = set(
                    session.execute(
                        select(ScannerFailure.tool).where(
                            ScannerFailure.audit_id == audit_uuid
                        )
                    ).scalars()
                )
                for tool, reason in failures.items():
                    if tool not in recorded:
                        session.add(
                            ScannerFailure(audit_id=audit_uuid, tool=tool, reason=reason)
                        )
                session.commit()
            finally:
                session.close()
        update: dict[str, Any] = {
            "stage": "scanners",
            "scanner_failures": failures,
            "completed": _stages(state, "scanners"),
        }
        if failures:
            # Partial results, not a failed audit: the report states the gap.
            update["incomplete"] = True
            update["incomplete_reason"] = (
                "scanner(s) failed: " + ", ".join(sorted(failures))
            )
            update["errors"] = [*state.errors, *(
                f"scanner {tool}: {reason}" for tool, reason in sorted(failures.items())
            )]
        return update

    return run_with_retry("scanners", work)


def route(state: AuditState, services: WorkflowServices | None = None) -> dict[str, Any]:
    """Decide whether the agent stages run at all."""
    services = services or WorkflowServices()

    def work() -> dict[str, Any]:
        target = "agents" if app_settings.agents_enabled and not state.budget_exceeded else "skip"
        return {
            "route": target,
            "stage": "route",
            "completed": _stages(state, "route"),
        }

    return run_with_retry("route", work)


def agents(state: AuditState, services: WorkflowServices | None = None) -> dict[str, Any]:
    """Run the agent crews inside the node; LangGraph does not know they exist."""
    services = services or WorkflowServices()

    def work() -> Any:
        session = services.session_factory()
        try:
            audit, _ = _load(session, state.audit_id)
            _set_stage(session, audit, Stage.AGENTS)
            _commit_and_announce(session, audit)
            return services.agents(session, uuid.UUID(state.audit_id))
        finally:
            session.close()

    update: dict[str, Any] = {"stage": "agents", "completed": _stages(state, "agents")}
    try:
        outcome = run_with_retry("agents", work)
    except BudgetExceeded as exc:
        logger.warning("audit %s stopped on budget: %s", state.audit_id, exc)
        return {
            **update,
            "budget_exceeded": True,
            "incomplete": True,
            "incomplete_reason": f"budget exhausted: {exc}"[:400],
            "errors": [*state.errors, "BudgetExceeded"],
        }
    except LLMError as exc:
        session = services.session_factory()
        try:
            unresolved = mark_unresolved(session, uuid.UUID(state.audit_id), UNRESOLVED_NOTE)
        finally:
            session.close()
        return {
            **update,
            "agent_errors": [*state.agent_errors, _failure_reason(exc)],
            "incomplete": True,
            "incomplete_reason": f"model failure: {exc}"[:400],
            "agent_unresolved": unresolved,
            "errors": [*state.errors, _failure_reason(exc)],
        }

    errors = [str(item) for item in getattr(outcome, "errors", ()) or ()]
    accepted = int(getattr(outcome, "accepted", 0) or 0)
    calls = int(getattr(outcome, "calls", 0) or 0)
    update.update({"agent_calls": calls, "agent_accepted": accepted, "agent_errors": errors})

    if any(_is_budget_failure(item) for item in errors):
        return {
            **update,
            "budget_exceeded": True,
            "incomplete": True,
            "incomplete_reason": "budget exhausted during the agent stage",
            "errors": [*state.errors, "BudgetExceeded"],
        }
    if errors:
        # A crew that reported a failure produced what it could; what it did not
        # verify stays unresolved rather than being counted as clean.
        session = services.session_factory()
        try:
            unresolved = mark_unresolved(session, uuid.UUID(state.audit_id), UNRESOLVED_NOTE)
        finally:
            session.close()
        return {
            **update,
            "incomplete": True,
            "incomplete_reason": errors[0][:400],
            "agent_unresolved": unresolved,
            "errors": [*state.errors, *errors[:3]],
        }
    return update


def validation(state: AuditState, services: WorkflowServices | None = None) -> dict[str, Any]:
    """Check every agent finding; on a model failure fall back to the deterministic pass."""
    services = services or WorkflowServices()

    def work(semantic: bool) -> Any:
        session = services.session_factory()
        try:
            audit, _ = _load(session, state.audit_id)
            _set_stage(session, audit, Stage.VALIDATION)
            _commit_and_announce(session, audit)
            return services.validation(
                session, uuid.UUID(state.audit_id), semantic=semantic
            )
        finally:
            session.close()

    update: dict[str, Any] = {"stage": "validation", "completed": _stages(state, "validation")}
    try:
        outcome = run_with_retry("validation", lambda: work(True))
    except BudgetExceeded as exc:
        return {
            **update,
            "budget_exceeded": True,
            "incomplete": True,
            "incomplete_reason": f"budget exhausted during validation: {exc}"[:400],
            "errors": [*state.errors, "BudgetExceeded"],
        }
    except LLMError as exc:
        # Only the model failure has a fallback. The deterministic pass cannot raise
        # an LLMError, and anything else escaping it is a bug - letting it propagate
        # is what keeps a broken validator visible instead of quietly degrading the
        # audit to "deterministic only".
        logger.warning("validation fell back to the deterministic pass: %s", exc)
        outcome = run_with_retry("validation", lambda: work(False))
        note = f"semantic check failed, deterministic only: {_failure_reason(exc)}"
        return {
            **update,
            "validation": {
                "checked": int(getattr(outcome, "checked", 0) or 0),
                "verified": int(getattr(outcome, "verified", 0) or 0),
                "rejected": int(getattr(outcome, "rejected", 0) or 0),
                "unresolved": int(getattr(outcome, "unresolved", 0) or 0),
            },
            "incomplete": True,
            "incomplete_reason": note[:400],
            "errors": [*state.errors, note],
        }

    return {
        **update,
        "validation": {
            "checked": int(getattr(outcome, "checked", 0) or 0),
            "verified": int(getattr(outcome, "verified", 0) or 0),
            "rejected": int(getattr(outcome, "rejected", 0) or 0),
            "unresolved": int(getattr(outcome, "unresolved", 0) or 0),
        },
    }


def risk(state: AuditState, services: WorkflowServices | None = None) -> dict[str, Any]:
    """Assess the audit's overall level and count what is being reported."""
    services = services or WorkflowServices()

    def work() -> dict[str, Any]:
        session = services.session_factory()
        try:
            rows = session.execute(
                select(Finding).where(Finding.audit_id == uuid.UUID(state.audit_id))
            ).scalars()
            counts: dict[str, int] = {}
            for finding in rows:
                counts[finding.severity.value] = counts.get(finding.severity.value, 0) + 1
            assessment = services.assess(session, uuid.UUID(state.audit_id))
            level = assessment.overall_level
            if isinstance(level, str):
                level = Severity(level)
            return {
                "stage": "risk",
                "risk": counts,
                "risk_level": level.value,
                "assessment_id": str(assessment.id),
                "completed": _stages(state, "risk"),
            }
        finally:
            session.close()

    return run_with_retry("risk", work)


def finalize(state: AuditState, services: WorkflowServices | None = None) -> dict[str, Any]:
    """Close the audit, labelling partial results as needing evidence."""
    services = services or WorkflowServices()

    def work() -> dict[str, Any]:
        incomplete = state.incomplete or state.budget_exceeded
        status = AuditStatus.NEEDS_EVIDENCE if incomplete else AuditStatus.COMPLETED
        session = services.session_factory()
        try:
            audit, _ = _load(session, state.audit_id)
            _set_stage(session, audit, Stage.REPORT, status=status)
            audit.finished_at = _now()
            record(
                session,
                actor="system",
                action="audit_incomplete" if incomplete else "audit_completed",
                object_type="audit",
                object_id=audit.id,
                before={"status": AuditStatus.RUNNING.value, "stage": "finalize"},
                after={
                    "status": status.value,
                    "reason": state.incomplete_reason,
                    "completed": state.completed,
                },
            )
            _commit_and_announce(session, audit)
            get_tracer().end_audit(
                audit_id=audit.id,
                status=status.value,
                cost_usd=audit.cost_usd,
                incomplete_reason=state.incomplete_reason,
            )
        finally:
            session.close()
        return {
            "status": status.value,
            "stage": "finalize",
            "completed": _stages(state, "finalize"),
        }

    return run_with_retry("finalize", work)


def review(state: AuditState, services: WorkflowServices | None = None) -> dict[str, Any]:
    """The human review gate (step 3.4).

    A critical assessment pauses the run here. The pause *is* the checkpoint: the
    node calls LangGraph's ``interrupt``, the graph writes its state and returns, and
    nothing else runs until a decision arrives. Creating the reviews row is guarded
    by "is there already a pending one for this assessment", because a resumed node
    re-executes from its first line.

    The decision itself is written by the API, not here: the reviewer's act has to be
    recorded even if the resume never happens, and the reviews row is the single
    place a resumed node can read it back.
    """
    services = services or WorkflowServices()
    level = state.risk_level
    update: dict[str, Any] = {
        "stage": "review",
        "completed": _stages(state, "review"),
    }
    if level != Severity.CRITICAL.value:
        return {**update, "review": "not_required"}

    session = services.session_factory()
    try:
        audit, _ = _load(session, state.audit_id)
        assessment_id = uuid.UUID(state.assessment_id) if state.assessment_id else None
        # The review for *this* assessment, whatever its status: a resumed node has
        # to find the row the reviewer decided on, not open a second one.
        query = select(Review).where(Review.audit_id == audit.id)
        if assessment_id is not None:
            query = query.where(Review.assessment_id == assessment_id)
        row = session.execute(
            query.order_by(Review.created_at.desc())
        ).scalars().first()
        if row is None:
            row = Review(
                audit_id=audit.id,
                assessment_id=assessment_id,
                status=ReviewStatus.PENDING,
                requested_at=_now(),
            )
            session.add(row)
            audit.status = AuditStatus.AWAITING_REVIEW
            record(
                session,
                actor="system",
                action="review_requested",
                object_type="audit",
                object_id=audit.id,
                before={"status": audit.status.value},
                after={"level": level, "assessment_id": str(assessment_id)},
            )
            _commit_and_announce(session, audit)
            session.refresh(row)
            logger.info("audit %s awaits review (review %s, level %s)", audit.id, row.id, level)
        payload = {
            "audit_id": state.audit_id,
            "review_id": str(row.id),
            "assessment_id": str(assessment_id) if assessment_id else None,
            "level": level,
        }
        review_id = str(row.id)

        for _ in range(5):
            session.refresh(row)
            if row.status is not ReviewStatus.PENDING:
                break
            interrupt(payload)  # pauses here; returns the resume value afterwards
        session.refresh(row)
        status = row.status
    finally:
        session.close()

    update.update(
        {
            "review_id": review_id,
            "review": status.value,
            # request_evidence sends the run back to validation with the note; every
            # other decision lets it finish.
            "review_decision": (
                "request_evidence" if status is ReviewStatus.NEEDS_EVIDENCE else None
            ),
        }
    )
    return update


def report(state: AuditState, services: WorkflowServices | None = None) -> dict[str, Any]:
    """Stub: report generation lands in step 3.4."""
    return {"report": "stub", "stage": "report", "completed": _stages(state, "report")}


NODES: dict[str, Callable[..., dict[str, Any]]] = {
    "intake": intake,
    "index": index,
    "scanners": scanners,
    "route": route,
    "agents": agents,
    "validation": validation,
    "risk": risk,
    "finalize": finalize,
    "review": review,
    "report": report,
}

__all__ = [
    "NODES",
    "WorkflowServices",
    "agents",
    "finalize",
    "index",
    "intake",
    "mark_unresolved",
    "report",
    "review",
    "risk",
    "route",
    "scanners",
    "validation",
]
