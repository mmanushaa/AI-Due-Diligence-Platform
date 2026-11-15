"""Step 3.8: tracing and the hard budget stop.

The budget was already enforced by the client (step 2.5) and surfaced by the workflow;
what these tests pin down is the pair the step is actually about - a recorded trace id
on every call, and a run that stops *cleanly* at its budget with results labelled
incomplete and the display cost still equal to the logged calls.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text

from app.core.config import Settings
from app.db.enums import (
    AgentName,
    AuditStatus,
    Category,
    EvidenceKind,
    FindingOrigin,
    Severity,
    SourceType,
    VerificationStatus,
    Verdict,
)
from app.db.models import AgentRun, Audit, Evidence, Finding, Repository
from app.db.session import get_session_factory
from app.llm.client import BudgetExceeded, Completion, LLMClient
from app.observability.tracing import Tracer, get_tracer, trace_id_for
from app.workflow.graph import postgres_checkpointer, start_or_resume
from app.workflow.nodes import WorkflowServices


class FakeProvider:
    """A model backend with no network: fixed usage, a fixed reported cost."""

    def __init__(self, *, reported_cost: str = "0.02", input_tokens: int = 1200, output_tokens: int = 400):
        self.reported_cost = Decimal(reported_cost)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.calls = 0

    def complete(self, *, model, messages, max_tokens, temperature, tools=None):
        self.calls += 1
        return Completion(
            text="ok",
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            reported_cost_usd=self.reported_cost,
        )


class RecordingTracer(Tracer):
    """A tracer that keeps what it was asked to record instead of sending it."""

    def __init__(self):
        super().__init__(None)
        self.enabled = True
        self.started: list[dict] = []
        self.generations: list[dict] = []
        self.ended: list[dict] = []

    def start_audit(self, **kwargs):
        self.started.append(kwargs)
        return trace_id_for(kwargs["audit_id"])

    def generation(self, **kwargs):
        self.generations.append(kwargs)

    def end_audit(self, **kwargs):
        self.ended.append(kwargs)

    def flush(self):
        pass


def test_settings(**overrides) -> Settings:
    values = {
        "litellm_api_key": "test-key",
        "llm_fallback_enabled": False,
        "llm_max_attempts": 1,
        "llm_max_output_tokens": 128,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture
def audit(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("print('hello')\n", encoding="utf-8")
    session = get_session_factory()()
    repository = Repository(
        name=f"pytest-trace-{uuid.uuid4().hex[:8]}",
        source_type=SourceType.ZIP,
        storage_path=str(tmp_path),
    )
    session.add(repository)
    session.commit()
    session.refresh(repository)
    row = Audit(repository_id=repository.id, status=AuditStatus.QUEUED)
    session.add(row)
    session.commit()
    session.refresh(row)
    audit_id = row.id
    session.close()

    yield audit_id

    session = get_session_factory()()
    session.execute(text("ALTER TABLE review_decisions DISABLE TRIGGER USER"))
    session.delete(session.get(Audit, audit_id))
    session.commit()
    session.execute(text("ALTER TABLE review_decisions ENABLE TRIGGER USER"))
    session.delete(session.get(Repository, repository.id))
    session.commit()
    session.close()


# --------------------------------------------------------------------------- #
# trace ids
# --------------------------------------------------------------------------- #


def test_a_trace_id_is_the_audit_id_without_dashes():
    audit_id = uuid.uuid4()

    assert trace_id_for(audit_id) == audit_id.hex
    assert trace_id_for(str(audit_id)) == audit_id.hex
    assert len(trace_id_for(audit_id)) == 32


def test_without_configuration_every_call_is_a_no_op():
    tracer = get_tracer()

    assert tracer.enabled is False
    # None of these may raise, and none may return a client.
    assert tracer.start_audit(audit_id=uuid.uuid4()) is not None
    tracer.generation(
        audit_id=uuid.uuid4(), agent="security", model="m", status="succeeded"
    )
    tracer.end_audit(audit_id=uuid.uuid4(), status="completed", cost_usd=0)
    tracer.flush()


# --------------------------------------------------------------------------- #
# a call is recorded twice: agent_runs and a generation
# --------------------------------------------------------------------------- #


def test_a_model_call_stores_the_trace_id_and_reports_a_generation(audit):
    tracer = RecordingTracer()
    session = get_session_factory()()
    try:
        client = LLMClient(
            session,
            audit,
            provider=FakeProvider(),
            settings=test_settings(),
            tracer=tracer,
        )
        result = client.complete("security", [{"role": "user", "content": "hi"}])
        session.commit()
        run = session.execute(
            select(AgentRun).where(AgentRun.audit_id == audit)
        ).scalars().one()
    finally:
        session.close()

    assert run.trace_id == trace_id_for(audit) == audit.hex
    assert run.model == result.model
    assert run.input_tokens == 1200 and run.output_tokens == 400
    assert run.cost_usd == Decimal("0.0200")
    assert run.latency_ms is not None and run.latency_ms >= 0

    generation = tracer.generations[-1]
    assert generation["audit_id"] == audit
    assert generation["agent"] == "security"
    assert generation["model"] == result.model
    assert generation["input_tokens"] == 1200
    assert generation["output_tokens"] == 400
    assert generation["cost_usd"] == Decimal("0.0200")
    assert generation["status"] == "succeeded"


def test_an_audit_run_opens_and_closes_one_trace(audit, monkeypatch):
    tracer = RecordingTracer()
    monkeypatch.setattr("app.workflow.nodes.get_tracer", lambda: tracer)

    run_with_stubs(audit, agents=None)

    assert len(tracer.started) == 1
    assert tracer.started[0]["audit_id"] == audit
    assert tracer.started[0]["repository"].startswith("pytest-trace-")
    assert len(tracer.ended) == 1
    assert tracer.ended[0]["status"] == "completed"
    assert tracer.ended[0]["incomplete_reason"] is None


# --------------------------------------------------------------------------- #
# the hard budget stop
# --------------------------------------------------------------------------- #


def test_the_client_refuses_to_call_once_the_budget_is_spent(audit):
    session = get_session_factory()()
    try:
        row = session.get(Audit, audit)
        row.budget_usd = Decimal("0.0001")
        session.commit()
        provider = FakeProvider(reported_cost="0.05")
        client = LLMClient(
            session, audit, provider=provider, settings=test_settings(), tracer=RecordingTracer()
        )
        client.complete("security", [{"role": "user", "content": "first"}])
        with pytest.raises(BudgetExceeded):
            client.complete("security", [{"role": "user", "content": "second"}])
    finally:
        session.close()

    # The second call never reached the provider: the check happens first.
    assert provider.calls == 1


def test_a_budget_stop_ends_the_run_cleanly_and_is_traced_as_incomplete(audit, monkeypatch):
    tracer = RecordingTracer()
    monkeypatch.setattr("app.workflow.nodes.get_tracer", lambda: tracer)

    session = get_session_factory()()
    session.get(Audit, audit).budget_usd = Decimal("0.0001")
    session.commit()
    session.close()

    def budgeted_agents(session, audit_id):
        """One real call, then the client refuses the next one."""
        client = LLMClient(
            session,
            audit_id,
            provider=FakeProvider(reported_cost="0.05"),
            settings=test_settings(),
            tracer=tracer,
        )
        client.complete("security", [{"role": "user", "content": "spend"}])
        client.complete("architecture", [{"role": "user", "content": "spend again"}])
        return SimpleNamespace(errors=(), accepted=1, calls=2)

    state = run_with_stubs(audit, agents=budgeted_agents)

    session = get_session_factory()()
    try:
        row = session.get(Audit, audit)
        spent = session.execute(
            select(func.coalesce(func.sum(AgentRun.cost_usd), 0)).where(AgentRun.audit_id == audit)
        ).scalar_one()
        calls = session.execute(
            select(func.count()).select_from(AgentRun).where(AgentRun.audit_id == audit)
        ).scalar_one()
    finally:
        session.close()

    assert state["budget_exceeded"] is True
    assert state["incomplete_reason"].startswith("budget exhausted")
    # Clean halt, not a crash: the audit is closed and labelled, not left running.
    assert row.status is AuditStatus.NEEDS_EVIDENCE
    assert row.finished_at is not None
    assert calls == 1  # the second call never happened
    assert row.cost_usd == spent == Decimal("0.0500")
    assert tracer.ended[0]["status"] == "needs_evidence"
    assert tracer.ended[0]["incomplete_reason"].startswith("budget exhausted")


def test_a_row_written_outside_the_client_still_moves_the_audit_total(audit):
    """The reconciliation is a property of the data, not of the one writer."""
    session = get_session_factory()()
    try:
        session.add(
            AgentRun(
                audit_id=audit,
                agent=AgentName.SECURITY,
                model="written-by-something-else",
                cost_usd=Decimal("0.0080"),
                status="ok",
            )
        )
        session.commit()
        session.expire_all()
        stored = session.get(Audit, audit).cost_usd
    finally:
        session.close()

    assert stored == Decimal("0.0080")


def test_cost_of_an_audit_equals_the_sum_of_its_agent_runs(audit):
    session = get_session_factory()()
    try:
        client = LLMClient(
            session,
            audit,
            provider=FakeProvider(reported_cost="0.0123"),
            settings=test_settings(),
            tracer=RecordingTracer(),
        )
        for _ in range(3):
            client.complete("security", [{"role": "user", "content": "hi"}])
        row = session.get(Audit, audit)
        summed = session.execute(
            select(func.coalesce(func.sum(AgentRun.cost_usd), 0)).where(AgentRun.audit_id == audit)
        ).scalar_one()
        stored = row.cost_usd
    finally:
        session.close()

    assert stored == summed == Decimal("0.0369")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def run_with_stubs(audit_id: uuid.UUID, agents=None):
    def index(session, audit, snapshot_path):
        return SimpleNamespace(included=1, excluded=0)

    def scanners(audit, snapshot_path, session_factory=None):
        return SimpleNamespace(failed=())

    def default_agents(session, audit):
        return SimpleNamespace(errors=(), accepted=0, calls=0)

    def validate(session, audit, semantic=True):
        return SimpleNamespace(checked=0, verified=0, rejected=0, unresolved=0)

    services = WorkflowServices(
        session_factory=get_session_factory(),
        index=index,
        scanners=scanners,
        agents=agents or default_agents,
        validation=validate,
    )
    with postgres_checkpointer() as saver:
        return start_or_resume(audit_id, services=services, checkpointer=saver)
