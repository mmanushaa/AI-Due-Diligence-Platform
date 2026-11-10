"""Step 2.6f: the model-free configuration stores the graph's cycles.

Two assertions matter here and they pull in opposite directions: the stage must
report the cycle the graph proves, and it must do it without spending a model
call. Both are checked against stored rows rather than the returned outcome, so a
finding that was "reported" into a list nothing saved fails.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.agents.deterministic import SOURCE_TOOL, run_deterministic_graph
from app.agents.tools import RepositoryTools
from app.db.enums import AuditStatus, Category, FindingOrigin, SourceType
from app.db.models import AgentRun, Audit, Finding, Repository
from app.db.session import get_session_factory

CYCLE = {
    "app/orders.py": '''"""Orders."""

from app.billing import invoice_total


def order_total(order):
    """Total one order."""
    return invoice_total(order)


def _unused():
    return 0
''',
    "app/billing.py": '''"""Billing."""

from app.orders import order_total


def invoice_total(order):
    """Total the invoice."""
    return order_total(order)
''',
}


@pytest.fixture
def cycle_audit(tmp_path: Path):
    """A repository whose two modules import each other, and an audit over it."""
    for path, content in CYCLE.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    session = get_session_factory()()
    repository = Repository(
        name=f"pytest-graph-{uuid.uuid4().hex[:8]}",
        source_type=SourceType.ZIP,
        storage_path=str(tmp_path),
    )
    session.add(repository)
    session.commit()
    session.refresh(repository)

    audit = Audit(repository_id=repository.id, status=AuditStatus.QUEUED)
    session.add(audit)
    session.commit()
    session.refresh(audit)
    audit_id = audit.id
    session.close()

    yield audit_id

    session = get_session_factory()()
    session.delete(session.get(Audit, audit_id))
    session.commit()
    session.delete(session.get(Repository, repository.id))
    session.commit()
    session.close()


def test_the_graph_stage_stores_the_proved_cycle_and_makes_no_model_call(cycle_audit):
    session = get_session_factory()()
    try:
        outcome = run_deterministic_graph(session, cycle_audit)

        assert outcome.errors == ()
        assert outcome.cycles == 1
        assert outcome.calls == 0
        assert outcome.cost_usd == 0

        findings = list(
            session.execute(select(Finding).where(Finding.audit_id == cycle_audit)).scalars()
        )
        assert len(findings) == 1
        finding = findings[0]
        assert finding.category is Category.ARCHITECTURE
        assert finding.origin is FindingOrigin.AGENT
        assert finding.source_tool == SOURCE_TOOL
        # The walk may start the cycle at either module; both have to be named.
        assert finding.title.endswith("import each other")
        assert "app/orders.py" in finding.title and "app/billing.py" in finding.title

        runs = list(
            session.execute(select(AgentRun).where(AgentRun.audit_id == cycle_audit)).scalars()
        )
        assert runs == []
        assert outcome.accepted == 1
    finally:
        session.close()


def test_the_stage_is_idempotent_because_the_fingerprint_already_exists(cycle_audit):
    session = get_session_factory()()
    try:
        first = run_deterministic_graph(session, cycle_audit)
        second = run_deterministic_graph(session, cycle_audit)

        assert first.accepted == 1
        assert second.accepted == 0
        stored = session.execute(
            select(Finding).where(Finding.audit_id == cycle_audit)
        ).scalars().all()
        assert len(stored) == 1
    finally:
        session.close()


def test_a_repository_with_no_cycle_stores_nothing_and_says_so(cycle_audit):
    session = get_session_factory()()
    try:
        # A repository of one module cannot form a cycle, whatever it imports.
        (Path(RepositoryTools(session, cycle_audit).snapshot_root) / "app" / "billing.py").unlink()
        (Path(RepositoryTools(session, cycle_audit).snapshot_root) / "app" / "orders.py").write_text(
            '"""Orders."""\n\n\ndef order_total(order):\n    """Total one order."""\n    return order\n',
            encoding="utf-8",
        )
        # The index was built by the first assertion's run only if it ran; build it
        # from the current files by dropping the audit's chunks and symbols.
        from sqlalchemy import delete

        from app.db.models import Chunk, File, Symbol

        for table in (Chunk, Symbol, File):
            session.execute(delete(table).where(table.audit_id == cycle_audit))
        session.commit()

        outcome = run_deterministic_graph(session, cycle_audit)
        assert outcome.errors == ()
        assert outcome.cycles == 0
        assert outcome.accepted == 0
        assert "0 cycle(s) proved by the module graph" in outcome.notes
    finally:
        session.close()
