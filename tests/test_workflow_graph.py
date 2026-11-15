"""Step 3.2: the audit workflow, LangGraph's order and a killed worker's resume.

The stages are injected, so these tests exercise the graph itself - order, retries,
failure paths, checkpoints - without docker images or a model. The resume test uses
the real Postgres checkpointer in its own ``workflow`` schema, because that is the
mechanism the step is about.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import select

from app.db.enums import (
    AuditStatus,
    Category,
    EvidenceKind,
    FindingOrigin,
    ScannerRunStatus,
    ScannerTool,
    Severity,
    SourceType,
    VerificationStatus,
    Verdict,
)
from app.db.models import Audit, Evidence, Finding, Repository
from app.db.session import get_session_factory
from app.llm.client import BudgetExceeded, LLMError
from app.workflow import nodes
from app.workflow.graph import (
    build_graph,
    pending_nodes,
    postgres_checkpointer,
    start_or_resume,
    thread_id_for,
)
from app.workflow.nodes import WorkflowServices
from app.workflow.state import AuditState

ALL_NODES = [
    "intake",
    "index",
    "scanners",
    "route",
    "agents",
    "validation",
    "risk",
    "review",
    "finalize",
    "report",
]
#: A run whose assessment is not critical never enters the review gate (step 3.4).
STAGES = [name for name in ALL_NODES if name != "review"]


class WorkerKilled(BaseException):
    """A worker that dies mid-run: not an Exception, so nothing retries or catches it."""


@dataclass
class Recorder:
    index: list = field(default_factory=list)
    scanners: list = field(default_factory=list)
    agents: list = field(default_factory=list)
    validation: list = field(default_factory=list)


def services_for(
    recorder: Recorder,
    *,
    scanner: object = None,
    agent: object = None,
    validation: object = None,
) -> WorkflowServices:
    """The real service shape, with the work replaced by a callable that may raise."""
    factory = get_session_factory()

    def index(session, audit_id, snapshot):
        recorder.index.append(str(audit_id))
        return SimpleNamespace(included=1, excluded=0)

    def scanners(audit_id, snapshot, session_factory=None):
        recorder.scanners.append(str(audit_id))
        if scanner is not None:
            return scanner() if callable(scanner) else scanner
        return SimpleNamespace(failed=())

    def agents(session, audit_id):
        recorder.agents.append(str(audit_id))
        if agent is not None:
            return agent() if callable(agent) else agent
        return SimpleNamespace(errors=(), accepted=1, calls=1)

    def default_validate(session, audit_id, semantic=True):
        recorder.validation.append(semantic)
        return SimpleNamespace(checked=1, verified=1, rejected=0, unresolved=0)

    return WorkflowServices(
        session_factory=factory,
        index=index,
        scanners=scanners,
        agents=agents,
        # Validation is a service, not a result producer: a test passes its own
        # callable with the real signature to exercise the fallback path.
        validation=validation or default_validate,
    )


@pytest.fixture
def repository(tmp_path):
    """A repository on disk with one readable file, and an audit over it."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("print('hello')\n", encoding="utf-8")

    session = get_session_factory()()
    row = Repository(
        name=f"pytest-workflow-{uuid.uuid4().hex[:8]}",
        source_type=SourceType.ZIP,
        storage_path=str(tmp_path),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    audit = Audit(repository_id=row.id, status=AuditStatus.QUEUED)
    session.add(audit)
    session.commit()
    session.refresh(audit)
    audit_id = audit.id
    session.close()

    yield audit_id

    session = get_session_factory()()
    session.delete(session.get(Audit, audit_id))
    session.commit()
    session.delete(session.get(Repository, row.id))
    session.commit()
    session.close()


def audit_row(audit_id) -> Audit:
    session = get_session_factory()()
    try:
        return session.get(Audit, audit_id)
    finally:
        session.close()


def status_of(finding_id) -> VerificationStatus:
    session = get_session_factory()()
    try:
        return session.get(Finding, finding_id).verification_status
    finally:
        session.close()


def add_agent_finding(audit_id) -> uuid.UUID:
    session = get_session_factory()()
    finding = Finding(
        audit_id=audit_id,
        category=Category.SECURITY,
        title="read_order returns another user's order",
        severity=Severity.HIGH,
        confidence=Decimal("0.80"),
        origin=FindingOrigin.AGENT,
        source_tool="security_agent",
        verification_status=VerificationStatus.AI_SUGGESTED,
        verdict=Verdict.ISSUE,
        reasoning="the route never resolves the caller",
        fingerprint=uuid.uuid4().hex,
    )
    session.add(finding)
    session.flush()
    session.add(
        Evidence(
            finding_id=finding.id,
            file_path="app/main.py",
            line_start=1,
            line_end=1,
            snippet="print('hello')",
            kind=EvidenceKind.PRIMARY,
        )
    )
    session.commit()
    finding_id = finding.id
    session.close()
    return finding_id


# --------------------------------------------------------------------------- #
# the graph itself
# --------------------------------------------------------------------------- #


def test_the_state_is_a_pydantic_model():
    from pydantic import BaseModel

    assert issubclass(AuditState, BaseModel)
    state = AuditState(audit_id=str(uuid.uuid4()))
    assert state.completed == [] and state.budget_exceeded is False


def test_every_node_in_the_step_exists():
    assert set(nodes.NODES) == set(ALL_NODES)


def test_the_graph_runs_every_stage_in_order(repository):
    recorder = Recorder()
    state = start_or_resume(
        repository,
        services=services_for(recorder),
        checkpointer=MemorySaver(),
    )

    assert state["completed"] == STAGES
    assert state["status"] == AuditStatus.COMPLETED.value
    assert recorder.index == [str(repository)]
    assert recorder.agents == [str(repository)]
    assert recorder.validation == [True]

    row = audit_row(repository)
    assert row.status is AuditStatus.COMPLETED
    assert row.workflow_thread_id == str(repository)
    assert row.finished_at is not None


def test_the_thread_id_is_the_audit_column(repository):
    thread = thread_id_for(get_session_factory(), repository)

    assert thread == str(repository)
    assert audit_row(repository).workflow_thread_id == thread


# --------------------------------------------------------------------------- #
# the required test: kill the worker mid-run, restart, resume where it stopped
# --------------------------------------------------------------------------- #


def _die():
    raise WorkerKilled("the worker was killed")


def test_a_killed_worker_resumes_at_the_last_completed_stage(repository):
    """The scanners node dies; the restarted run starts there, not at intake."""
    first = Recorder()
    with postgres_checkpointer() as saver:
        with pytest.raises(WorkerKilled):
            start_or_resume(
                repository,
                services=services_for(first, scanner=_die),
                checkpointer=saver,
            )

    # The nodes before the kill ran once; the one that died did not complete.
    assert first.index == [str(repository)]
    assert first.scanners == [str(repository)]
    assert first.agents == []
    with postgres_checkpointer() as saver:
        assert pending_nodes(repository, checkpointer=saver) == ["scanners"]

    # The worker comes back and the same audit is handed to it again.
    second = Recorder()
    with postgres_checkpointer() as saver:
        state = start_or_resume(
            repository, services=services_for(second), checkpointer=saver
        )

    assert state["completed"] == STAGES
    assert state["status"] == AuditStatus.COMPLETED.value
    # Nothing before the crash ran a second time ...
    assert second.index == []
    assert second.scanners == [str(repository)]
    # ... and everything after it ran exactly once.
    assert second.agents == [str(repository)]
    assert second.validation == [True]
    with postgres_checkpointer() as saver:
        assert pending_nodes(repository, checkpointer=saver) == []


def test_a_finished_run_is_not_started_again(repository):
    """A redelivered message after a completed run must not redo the audit."""
    saver = MemorySaver()
    first = Recorder()
    start_or_resume(repository, services=services_for(first), checkpointer=saver)

    second = Recorder()
    state = start_or_resume(
        repository, services=services_for(second), checkpointer=saver
    )

    assert state["completed"] == STAGES
    assert second.index == [] and second.scanners == [] and second.agents == []


# --------------------------------------------------------------------------- #
# the TRD's failure paths
# --------------------------------------------------------------------------- #


def test_a_failed_scanner_marks_that_source_failed_and_the_run_continues(repository):
    recorder = Recorder()
    failed = SimpleNamespace(
        failed=(
            SimpleNamespace(
                tool=ScannerTool.SEMGREP,
                status=ScannerRunStatus.FAILED,
                error="semgrep image missing",
            ),
        )
    )
    state = start_or_resume(
        repository,
        services=services_for(recorder, scanner=failed),
        checkpointer=MemorySaver(),
    )

    assert state["scanner_failures"] == {"semgrep": "semgrep image missing"}
    assert state["incomplete"] is True
    assert "semgrep" in (state["incomplete_reason"] or "")
    # The run continued through the agent stages and closed as needing evidence.
    assert state["completed"] == STAGES
    assert recorder.agents == [str(repository)]
    assert audit_row(repository).status is AuditStatus.NEEDS_EVIDENCE


def test_budget_exceeded_stops_cleanly_with_partial_results(repository):
    recorder = Recorder()

    def broke() -> None:
        raise BudgetExceeded(repository, Decimal("0.01"), Decimal("0.02"))

    state = start_or_resume(
        repository,
        services=services_for(recorder, agent=broke),
        checkpointer=MemorySaver(),
    )

    assert state["budget_exceeded"] is True
    assert state["incomplete"] is True
    assert "budget" in (state["incomplete_reason"] or "").lower()
    # Partial results are kept: what ran before the budget is still in the state ...
    assert state["files_included"] == 1
    assert state["completed"] == [*STAGES[:4], "agents", "finalize", "report"]
    # ... the validation stage is skipped, not attempted with no money ...
    assert recorder.validation == []
    # ... and the audit is labelled, not failed.
    assert state["status"] == AuditStatus.NEEDS_EVIDENCE.value
    assert audit_row(repository).status is AuditStatus.NEEDS_EVIDENCE


def test_an_llm_failure_marks_affected_findings_unresolved(repository):
    recorder = Recorder()
    finding_id = add_agent_finding(repository)

    def models_are_down() -> None:
        raise LLMError("gateway returned 502 twice")

    state = start_or_resume(
        repository,
        services=services_for(recorder, agent=models_are_down),
        checkpointer=MemorySaver(),
    )

    assert state["incomplete"] is True
    assert "LLMError" in (state["errors"] or [""])[0]
    assert status_of(finding_id) is VerificationStatus.UNRESOLVED
    assert state["agent_unresolved"] == 1
    assert audit_row(repository).status is AuditStatus.NEEDS_EVIDENCE


def test_a_failed_semantic_validation_falls_back_to_the_deterministic_pass(repository):
    recorder = Recorder()
    calls: list[bool] = []

    def validate(session, audit_id, semantic=True):
        calls.append(semantic)
        if semantic:
            raise LLMError("validator could not reach the model")
        return SimpleNamespace(checked=1, verified=1, rejected=0, unresolved=0)

    state = start_or_resume(
        repository,
        services=services_for(recorder, validation=validate),
        checkpointer=MemorySaver(),
    )

    # The policy retries the semantic pass once, then the node falls back.
    assert calls == [True, True, False]
    assert state["incomplete"] is True
    assert "deterministic only" in (state["incomplete_reason"] or "")
    assert state["validation"]["verified"] == 1
    assert state["completed"] == STAGES


def test_a_programming_error_in_validation_is_not_downgraded(repository):
    """Only model failures get the deterministic fallback; a bug must surface."""

    def validate(session, audit_id, semantic=True):
        raise TypeError("broken stub: validation returned no outcome")

    with pytest.raises(TypeError, match="broken stub"):
        start_or_resume(
            repository,
            services=services_for(Recorder(), validation=validate),
            checkpointer=MemorySaver(),
        )


def test_the_agents_node_is_the_only_place_agents_run(repository):
    """LangGraph knows a node named agents, not a crew."""
    recorder = Recorder()
    start_or_resume(repository, services=services_for(recorder), checkpointer=MemorySaver())

    assert recorder.agents == [str(repository)]
    graph = build_graph(MemorySaver())
    assert "security" not in graph.get_graph().nodes
    assert "agents" in graph.get_graph().nodes


# --------------------------------------------------------------------------- #
# the Celery task only starts or resumes
# --------------------------------------------------------------------------- #


def test_the_celery_task_only_starts_or_resumes(monkeypatch):
    from app.jobs import tasks

    calls: list[str] = []

    def fake_start(audit_id):
        calls.append(str(audit_id))
        return {
            "audit_id": str(audit_id),
            "status": "completed",
            "stage": "report",
            "completed": ["intake"],
        }

    monkeypatch.setattr(tasks, "start_or_resume", fake_start)
    audit_id = uuid.uuid4()

    result = tasks.run_audit_task(str(audit_id))

    assert calls == [str(audit_id)]
    assert result["status"] == "completed"
    assert result["completed"] == ["intake"]
