"""Step 2.6e: the agent event log.

The model is a script, not a provider. A fake crew replays prepared replies through
the crew's *own* guardrail, which is what CrewAI does between retries, so what is
exercised here is the recording and not CrewAI's retry loop. The stage itself runs
end to end, which is what lets the test say the run's result did not change: the
same finding is stored, nothing extra is stored, and the outcome counts match.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.agents.architecture import AGENT_ROLE, build_crew, run_architecture_agent
from app.agents.tools import RepositoryTools
from app.core.config import Settings
from app.db.enums import AgentEventType, AuditStatus, SourceType
from app.db.models import AgentEvent, Audit, Finding, Repository
from app.db.session import get_session_factory
from app.indexing.service import index_audit, index_graph
from app.intake.filtering import index_snapshot
from app.llm.client import LLMClient
from app.retrieval.embeddings import HashingEmbeddingProvider, embed_chunks

REVIEWED_FILES = {
    "app/__init__.py": '"""Package."""\n',
    "app/main.py": '''"""Entry point."""

from app.store import load


def run() -> str:
    """Start the service."""
    return load("1")
''',
    "app/store.py": '''"""Data access."""


def load(key: str) -> str:
    """Load one row."""
    return key
''',
}


def settings_for(**overrides) -> Settings:
    """Deterministic client settings: one attempt, no fallback, no waiting."""
    base = {
        "llm_default_model": "gpt-4o-mini",
        "llm_triage_model": "gpt-4o-mini",
        "llm_fallback_enabled": False,
        "llm_max_attempts": 1,
        "llm_retry_base_seconds": 0.0,
        "llm_retry_max_seconds": 0.0,
    }
    base.update(overrides)
    return Settings(**base)


def reply(*findings: dict) -> str:
    """A model reply containing exactly these findings."""
    return json.dumps({"findings": list(findings)})


def module_finding(**overrides) -> dict:
    """One finding that cites lines this repository really has."""
    base = {
        "category": "architecture",
        "title": "Store module both loads and formats the row",
        "file": "app/store.py",
        "line_start": 4,
        "line_end": 5,
        "severity": "low",
        "confidence": 0.8,
        "reasoning": "app/store.py:4-5 hands the raw row back, so every caller re-implements its shape.",
        "recommendation": "Return a value object instead of the row.",
    }
    base.update(overrides)
    return base


class ReplayingCrew:
    """A crew whose model is a script: each reply goes through the real guardrail.

    ``kickoff`` does what CrewAI does around a guardrail — one tool call, then each
    reply in turn, retrying while it is refused — and returns the last reply as the
    task's output. Nothing here decides anything the stage would not have decided.
    """

    def __init__(self, crew, replies: tuple[str, ...]) -> None:
        self._crew = crew
        self._replies = replies
        self.verdicts: list[bool] = []

    def kickoff(self):
        agent = self._crew.agents[0]
        task = self._crew.tasks[0]
        agent.tools[0].run(query="store module shape")  # the model reads first
        for answer in self._replies[:-1]:
            passed, _ = task.guardrail(answer)
            self.verdicts.append(passed)
        passed, _ = task.guardrail(self._replies[-1])
        self.verdicts.append(passed)
        from types import SimpleNamespace

        return SimpleNamespace(tasks_output=[SimpleNamespace(raw=self._replies[-1])])


def crew_factory_for(replies: tuple[str, ...]):
    """A factory that builds the real crew and scripts its answers."""

    def factory(tools, llm, facts, calls):
        return ReplayingCrew(build_crew(tools, llm, facts, calls), replies)

    return factory


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory) -> Path:
    """One small repository with a one-way import, so no cycle is involved."""
    root = tmp_path_factory.mktemp("agent-events-repo")
    for path, content in REVIEWED_FILES.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


@pytest.fixture
def audit(snapshot):
    """A fresh, fully indexed audit over that repository, removed afterwards."""
    session = get_session_factory()()
    repository = Repository(
        name=f"pytest-events-{uuid.uuid4().hex[:8]}",
        source_type=SourceType.ZIP,
        storage_path=str(snapshot),
    )
    session.add(repository)
    session.commit()
    session.refresh(repository)

    audit = Audit(repository_id=repository.id, status=AuditStatus.QUEUED)
    session.add(audit)
    session.commit()
    session.refresh(audit)

    index_snapshot(session, audit.id, snapshot)
    index_audit(session, audit.id, snapshot_root=snapshot)
    index_graph(session, audit.id, snapshot_root=snapshot)
    embed_chunks(session, audit.id, provider=HashingEmbeddingProvider(), batch_size=8)
    session.close()

    yield audit.id

    session = get_session_factory()()
    session.delete(session.get(Audit, audit.id))
    session.commit()
    session.delete(session.get(Repository, repository.id))
    session.commit()
    session.close()


def events_of(session, audit_id, event_type: AgentEventType) -> list[AgentEvent]:
    """This audit's events of one kind, in the order they were written."""
    return list(
        session.execute(
            select(AgentEvent)
            .where(AgentEvent.audit_id == audit_id, AgentEvent.event_type == event_type)
            .order_by(AgentEvent.created_at, AgentEvent.attempt)
        ).scalars()
    )


def test_two_refusals_then_an_answer_are_all_recorded(audit):
    """The replies that were refused are the rows worth having, so keep them."""
    first_refusal = "I found the problem but will not format it as JSON."
    second_refusal = "Here is a prose summary instead."
    answer = reply(module_finding())
    replies = (first_refusal, second_refusal, answer)

    session = get_session_factory()()
    try:
        client = LLMClient(session, audit, settings=settings_for())
        outcome = run_architecture_agent(
            session, audit, client=client, crew_factory=crew_factory_for(replies)
        )

        refusals = events_of(session, audit, AgentEventType.GUARDRAIL_FAILURE)
        calls = events_of(session, audit, AgentEventType.TOOL_CALL)
        finals = events_of(session, audit, AgentEventType.FINAL_OUTPUT)

        # the two refusals, with the text that was refused and which try it was
        assert [row.payload["raw"] for row in refusals] == [first_refusal, second_refusal]
        assert [row.attempt for row in refusals] == [1, 2]
        assert all("not the JSON object" in row.payload["reason"] for row in refusals)
        assert all(row.agent is AGENT_ROLE for row in refusals)

        # exactly one tool call: what was asked for, and how much came back
        assert len(calls) == 1
        assert calls[0].tool_name == "retrieve_context"
        assert calls[0].payload["arguments"] == {"query": "store module shape"}
        assert calls[0].payload["result_chars"] > 0

        # the reply the task ended with
        assert len(finals) == 1
        assert finals[0].payload == {"task": "review", "raw": answer}

        # the run's result is unchanged by any of this
        assert outcome.errors == ()
        assert outcome.accepted == 1
        assert outcome.rejected == 0
        stored = list(
            session.execute(select(Finding).where(Finding.audit_id == audit)).scalars()
        )
        assert [row.title for row in stored] == ["Store module both loads and formats the row"]
    finally:
        session.close()


def test_a_credential_in_a_refused_reply_is_masked_before_it_is_stored(audit):
    """The reply is kept; the token inside it is not. Same masker as the scanners."""
    token = "ghp_" + "A" * 36
    refused = f"the repository key is {token} and I will not answer in JSON"
    replies = (refused, reply(module_finding()))

    session = get_session_factory()()
    try:
        client = LLMClient(session, audit, settings=settings_for())
        run_architecture_agent(
            session, audit, client=client, crew_factory=crew_factory_for(replies)
        )

        refusals = events_of(session, audit, AgentEventType.GUARDRAIL_FAILURE)
        stored = json.dumps([row.payload for row in refusals])

        assert token not in stored
        assert "***REDACTED***" in stored
        assert "I will not answer in JSON" in stored  # the useful part survives
    finally:
        session.close()
