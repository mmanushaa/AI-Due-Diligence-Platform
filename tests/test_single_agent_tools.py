"""Step 2.6f: the single agent that chooses its own tools.

The configuration is only a fair comparison if it really has the crews' tools and
limits, so that is asserted against the built crew rather than against the config
block. The rest is the storage path: what it writes, under which ``source_tool``,
and what an answer outside the contract does.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agents.crew import GatewayLLM
from app.agents.single_tools import SOURCE_TOOL, build_crew, run_single_agent_tools
from app.agents.tools import RepositoryTools
from app.core.config import Settings
from app.db.enums import AuditStatus, Category, SourceType
from app.db.models import Audit, Finding, Repository
from app.db.session import get_session_factory
from app.llm.client import LLMClient

REVIEWED = {
    "app/auth.py": '''"""Authentication helpers."""


def current_user(token):
    if not token:
        raise PermissionError("missing token")
    return {"user_id": 1}
''',
    "app/orders.py": '''"""Order routes."""

from app.auth import current_user


def read_order(conn, order_id):
    """Return one order."""
    return conn.execute("SELECT id FROM orders WHERE id = ?", (order_id,)).fetchone()
''',
}

FINDING = {
    "category": "security",
    "title": "Order route reads any order without checking the caller",
    "file": "app/orders.py",
    "line_start": 7,
    "line_end": 8,
    "severity": "high",
    "confidence": 0.9,
    "reasoning": "app/auth.py defines current_user and this route never calls it.",
    "recommendation": "Resolve the caller and scope the query to their user id.",
}


def reply(*findings: dict) -> str:
    return json.dumps({"findings": list(findings)})


def settings_for() -> Settings:
    return Settings(
        llm_default_model="gpt-4o-mini",
        llm_triage_model="gpt-4o-mini",
        llm_fallback_model="gpt-4o",
        llm_fallback_enabled=False,
        llm_max_attempts=1,
        llm_retry_base_seconds=0.0,
        llm_retry_max_seconds=0.0,
    )


class UnreachableProvider:
    """A provider that fails the test if the stage reaches it."""

    def complete(self, *, model, messages, max_tokens, temperature):  # pragma: no cover
        raise AssertionError("this test must not call the model")


class ScriptedCrew:
    """A crew whose kickoff returns one prepared answer."""

    def __init__(self, raw: str) -> None:
        self.raw = raw

    def kickoff(self) -> SimpleNamespace:
        return SimpleNamespace(tasks_output=[SimpleNamespace(raw=self.raw)])


@pytest.fixture
def audit(tmp_path: Path):
    for path, content in REVIEWED.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    session = get_session_factory()()
    repository = Repository(
        name=f"pytest-single-tools-{uuid.uuid4().hex[:8]}",
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
    session.delete(session.get(Audit, audit_id))
    session.commit()
    session.delete(session.get(Repository, repository.id))
    session.commit()
    session.close()


def run(audit_id, raw: str):
    """Run the stage with a scripted crew and a client that must not be called."""
    session = get_session_factory()()
    client = LLMClient(
        session, audit_id, provider=UnreachableProvider(), settings=settings_for()
    )
    outcome = run_single_agent_tools(
        session,
        audit_id,
        client=client,
        crew_factory=lambda tools, llm, calls: ScriptedCrew(raw),
    )
    return outcome, session


def test_the_crew_has_the_crews_tools_turns_and_guardrail(audit):
    session = get_session_factory()()
    try:
        tools = RepositoryTools(session, audit)
        llm = GatewayLLM(SimpleNamespace(settings=SimpleNamespace(llm_default_model="gpt-4o-mini")), agent=None)
        crew = build_crew(tools, llm, [])

        assert len(crew.tasks) == 1
        assert [tool.name for tool in crew.agents[0].tools] == [
            "retrieve_context",
            "get_symbol",
            "read_finding",
        ]
        assert crew.agents[0].max_iter == 8
        assert crew.tasks[0].guardrail_max_retries == 2
    finally:
        session.close()


def test_a_valid_answer_is_stored_under_this_stage(audit):
    outcome, session = run(audit, reply(FINDING))
    try:
        findings = list(
            session.execute(select(Finding).where(Finding.audit_id == audit)).scalars()
        )
        assert outcome.accepted == 1
        assert outcome.errors == ()
        assert len(findings) == 1
        assert findings[0].source_tool == SOURCE_TOOL
        assert findings[0].category is Category.SECURITY
    finally:
        session.close()


def test_an_answer_outside_the_contract_is_rejected_rather_than_stored(audit):
    outcome, session = run(audit, "I found nothing worth reporting.")
    try:
        assert outcome.accepted == 0
        assert outcome.rejected == 1
        assert outcome.dropped == 1
        assert outcome.notes and "unparseable" in outcome.notes[0]
        findings = session.execute(select(Finding).where(Finding.audit_id == audit)).scalars().all()
        assert findings == []
    finally:
        session.close()


def test_a_finding_that_cites_a_line_outside_the_file_is_not_stored(audit):
    outcome, session = run(audit, reply({**FINDING, "line_start": 9_000, "line_end": 9_001}))
    try:
        assert outcome.accepted == 0
        assert outcome.rejected == 1
        assert any("rejected by the validator" in note for note in outcome.notes)
    finally:
        session.close()
