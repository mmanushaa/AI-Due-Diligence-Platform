"""Step 2.9: the architecture agent, its facts and its validation gate.

The model is replaced by a fake crew, so what is tested here is everything that is
ours: the graph facts read out of step 2.2's tables (cycles with their real import
lines, entry points, large modules, layering), the guardrail that makes the crew read
before it judges, and the rule that a finding citing a file the repository does not
have never reaches the finding table.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agents import STAGES
from app.agents.architecture import (
    AGENT_ROLE,
    LARGE_MODULE_LINES,
    ArchitectureFacts,
    ArchitectureOutcome,
    build_crew,
    read_facts,
    run_architecture_agent,
    validate_architecture_report,
)
from app.agents.crew import GatewayLLM, gateway_tools, messages_for_gateway, read_only_tools
from app.agents.multi import run_multi_agent
from app.agents.prompts import load_prompt
from app.agents.schema import parse_report
from app.agents.tools import RepositoryTools
from app.core.config import Settings
from app.db.enums import AuditStatus, EvalConfig, SourceType
from app.db.models import Audit, Finding, Repository
from app.db.session import get_session_factory
from app.indexing.service import index_audit, index_graph
from app.intake.filtering import index_snapshot
from app.retrieval.embeddings import HashingEmbeddingProvider, embed_chunks

BIG_MODULE = '"""One very large module."""\n' + "\n".join(
    f"VALUE_{index} = {index}" for index in range(LARGE_MODULE_LINES + 20)
)

REVIEWED_FILES = {
    "app/__init__.py": '"""Package."""\n',
    "app/routes/__init__.py": '"""Routes."""\n',
    "app/main.py": '''"""Entry point."""

from app.routes.orders import handler


def run() -> None:
    """Start the service."""
    handler()
''',
    "app/store.py": '''"""Data access."""

from app.routes.orders import handler


def load(key: str) -> str:
    """Load one row."""
    return handler()
''',
    "app/routes/orders.py": '''"""Order routes."""

from app.store import load


def handler() -> str:
    """Serve one order."""
    return load("1")
''',
    "app/big.py": BIG_MODULE,
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


@dataclass
class FakeCrew:
    """A crew whose single task answers with prepared text."""

    answer: str = '{"findings": []}'
    error: Exception | None = None
    seen: list = field(default_factory=list)

    def kickoff(self):
        """Answer as CrewAI would."""
        if self.error is not None:
            raise self.error
        return SimpleNamespace(tasks_output=[SimpleNamespace(raw=self.answer)])


def crew_factory_for(crew: FakeCrew):
    """A factory that records what the crew was handed and returns the fake."""

    def factory(tools, llm, facts, calls):
        crew.seen.append({"tools": tools, "llm": llm, "facts": facts, "calls": calls})
        return crew

    return factory


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory) -> Path:
    """One small repository with a cycle, a layering violation and a big file."""
    root = tmp_path_factory.mktemp("architecture-repo")
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
        name=f"pytest-architecture-{uuid.uuid4().hex[:8]}",
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


def facts_of(audit_id):
    """The computed facts, with the session closed again."""
    session = get_session_factory()()
    try:
        return read_facts(session, audit_id, RepositoryTools(session, audit_id))
    finally:
        session.close()


def cycle_finding(**overrides) -> dict:
    """A finding that cites the storage side of the planted cycle."""
    base = {
        "category": "architecture",
        "title": "app.store and app.routes.orders import each other",
        "file": "app/store.py",
        "line_start": 3,
        "line_end": 3,
        "severity": "medium",
        "confidence": 0.9,
        "reasoning": "app/store.py imports app.routes.orders, which imports app/store.py back.",
        "recommendation": "Move the shared load into a module both can import.",
        "evidence_quote": "from app.routes.orders import handler",
    }
    base.update(overrides)
    return base


def reply(*findings: dict) -> str:
    return json.dumps({"findings": list(findings)})


def _output(raw: str) -> SimpleNamespace:
    """What CrewAI hands a guardrail: an object with the task's raw text."""
    return SimpleNamespace(raw=raw)


# --------------------------------------------------------------------------- #
# registration and prompt
# --------------------------------------------------------------------------- #


def test_the_configuration_runs_both_crews_not_just_security():
    assert STAGES[EvalConfig.MULTI_AGENT] is run_multi_agent


def test_the_prompt_has_every_section_the_crew_reads():
    prompt = load_prompt("architecture")

    assert {"role", "goal", "backstory", "review", "json_contract"} <= set(prompt)
    assert "{{facts}}" in prompt["review"]
    assert "architecture" in prompt["json_contract"]


# --------------------------------------------------------------------------- #
# the facts
# --------------------------------------------------------------------------- #


def test_the_cycle_is_reported_with_the_import_line_that_closes_it(audit):
    facts = facts_of(audit)

    assert len(facts.cycles) == 1
    edges = facts.cycles[0].edges
    assert {(edge.from_path, edge.to_path) for edge in edges} == {
        ("app/store.py", "app/routes/orders.py"),
        ("app/routes/orders.py", "app/store.py"),
    }
    assert {edge.line for edge in edges} == {3}  # both imports are the third line
    assert all(edge.statement.startswith("from app.") for edge in edges)


def test_a_module_nothing_imports_is_an_entry_point(audit):
    facts = facts_of(audit)

    assert "app/main.py" in facts.entry_points
    assert "app/store.py" not in facts.entry_points


def test_a_module_over_the_floor_and_the_median_is_large(audit):
    facts = facts_of(audit)

    assert [module.path for module in facts.large_modules] == ["app/big.py"]
    assert facts.large_modules[0].ratio >= 3.0


def test_storage_importing_the_route_layer_is_a_layering_violation(audit):
    facts = facts_of(audit)

    assert [(item.edge.from_path, item.edge.to_path) for item in facts.layer_edges] == [
        ("app/store.py", "app/routes/orders.py")
    ]
    assert facts.layer_edges[0].from_layer == "storage"
    assert facts.layer_edges[0].to_layer == "interface"


def test_the_facts_are_rendered_so_the_model_can_cite_them(audit):
    rendered = facts_of(audit).render()

    assert "app/store.py:3 imports app/routes/orders.py" in rendered
    assert "Circular imports" in rendered
    assert "Entry points" in rendered


# --------------------------------------------------------------------------- #
# validation: a cited file has to exist
# --------------------------------------------------------------------------- #


def test_a_finding_about_a_file_the_repository_lacks_is_rejected(audit):
    session = get_session_factory()()
    tools = RepositoryTools(session, audit)
    try:
        report = parse_report(
            reply(
                cycle_finding(),
                cycle_finding(
                    title="A module that was never written imports half the codebase",
                    file="app/invented.py",
                ),
            )
        )
        result = validate_architecture_report(report, tools)
    finally:
        session.close()

    assert len(result.accepted) == 1
    assert result.accepted[0].file == "app/store.py"
    assert "is not in this repository" in result.rejected[0].reason


def test_a_security_finding_is_not_this_stage_s_to_store(audit):
    session = get_session_factory()()
    tools = RepositoryTools(session, audit)
    try:
        report = parse_report(reply(cycle_finding(category="security")))
        result = validate_architecture_report(report, tools)
    finally:
        session.close()

    assert result.accepted == ()
    assert "not architecture" in result.rejected[0].reason


# --------------------------------------------------------------------------- #
# the stage
# --------------------------------------------------------------------------- #


def test_the_stage_stores_the_cycle_and_counts_what_it_rejected(audit):
    session = get_session_factory()()
    crew = FakeCrew(
        answer=reply(cycle_finding(), cycle_finding(file="src/nothing.ts", line_start=1, title="A module the repository does not have"))
    )
    try:
        outcome = run_architecture_agent(
            session, audit, crew_factory=crew_factory_for(crew)
        )
        stored = list(
            session.execute(select(Finding).where(Finding.audit_id == audit)).scalars()
        )
    finally:
        session.close()

    assert isinstance(outcome, ArchitectureOutcome)
    assert outcome.accepted == 1
    assert outcome.rejected == 1
    assert outcome.cycles == 1
    assert any("does not have" in note for note in outcome.notes)
    assert [(row.source_tool, row.category.value) for row in stored] == [
        ("architecture_agent", "architecture")
    ]


def test_a_cycle_the_model_described_is_not_stored_twice(audit):
    """The graph fills gaps; it does not repeat what the model already said."""
    session = get_session_factory()()
    try:
        outcome = run_architecture_agent(
            session, audit, crew_factory=crew_factory_for(FakeCrew(answer=reply(cycle_finding())))
        )
        stored = list(
            session.execute(select(Finding).where(Finding.audit_id == audit)).scalars()
        )
    finally:
        session.close()

    assert outcome.accepted == 1
    assert len(stored) == 1
    assert not any("from the graph" in note for note in outcome.notes)


def test_an_unparseable_answer_is_dropped_not_stored(audit):
    session = get_session_factory()()
    try:
        outcome = run_architecture_agent(
            session, audit, crew_factory=crew_factory_for(FakeCrew(answer="I found a cycle!"))
        )
    finally:
        session.close()

    # The reply is dropped, but the cycle the graph proved is still reported.
    assert outcome.dropped == 1
    assert outcome.accepted == 1
    assert any("from the graph" in note for note in outcome.notes)


def test_a_failing_crew_does_not_fail_the_audit(audit):
    session = get_session_factory()()
    try:
        outcome = run_architecture_agent(
            session, audit, crew_factory=crew_factory_for(FakeCrew(error=RuntimeError("boom")))
        )
    finally:
        session.close()

    assert "boom" in outcome.errors[0]
    assert outcome.accepted == 1  # the cycle came from the facts, not the model
    assert any("from the graph" in note for note in outcome.notes)


# --------------------------------------------------------------------------- #
# the crew
# --------------------------------------------------------------------------- #


def test_the_crew_is_one_agent_one_task_with_the_read_only_tools(audit):
    session = get_session_factory()()
    tools = RepositoryTools(session, audit)
    try:
        facts = read_facts(session, audit, tools)
        calls: list[str] = []
        llm = GatewayLLM(_client_for(session, audit), agent=AGENT_ROLE)
        crew = build_crew(tools, llm, facts, calls)
        assert len(crew.agents) == 1
        assert len(crew.tasks) == 1
        # Both ends of the cycle are in the facts, whichever the walk started on.
        assert "imports app/store.py" in crew.tasks[0].description
        assert "imports app/routes/orders.py" in crew.tasks[0].description
        assert {tool.name for tool in crew.agents[0].tools} == {
            "retrieve_context",
            "get_symbol",
            "read_finding",
        }
        guardrail = crew.tasks[0].guardrail
        rejected, feedback = guardrail(_output("I looked and found a cycle."))
        assert rejected is False
        assert "JSON object" in feedback

        # An empty report is allowed: the graph's cycles are filled in anyway.
        assert guardrail(_output('{"findings": []}'))[0] is True

        rejected, feedback = guardrail(_output(reply(cycle_finding())))
        assert rejected is False
        assert "have not read any code" in feedback

        calls.append("retrieve_context")
        assert guardrail(_output(reply(cycle_finding())))[0] is True
    finally:
        session.close()


def test_a_repository_with_nothing_to_report_may_answer_empty(audit):
    """Forcing a read on a repository with no cycles failed two stages outright."""
    session = get_session_factory()()
    tools = RepositoryTools(session, audit)
    try:
        crew = build_crew(
            tools,
            GatewayLLM(_client_for(session, audit), agent=AGENT_ROLE),
            ArchitectureFacts(),
            [],
        )
        assert crew.tasks[0].guardrail(_output('{"findings": []}'))[0] is True
    finally:
        session.close()


def test_the_tools_a_crew_gets_cannot_write_or_shell_out(audit):
    session = get_session_factory()()
    tools = RepositoryTools(session, audit)
    try:
        calls: list[str] = []
        crew_tools = read_only_tools(tools, calls)
        assert [tool.name for tool in crew_tools] == [
            "retrieve_context",
            "get_symbol",
            "read_finding",
        ]
        crew_tools[0].run(query="where is the cycle")
        assert calls == ["retrieve_context"]
    finally:
        session.close()


def test_the_shared_adapter_still_drops_the_strict_flag():
    assert "strict" not in gateway_tools(
        [{"type": "function", "function": {"name": "f", "parameters": {}, "strict": True}}]
    )[0]["function"]
    assert messages_for_gateway([{"role": "tool", "content": "x", "tool_call_id": "c1"}])[0][
        "tool_call_id"
    ] == "c1"


def test_the_composed_stage_adds_both_crews_outcomes_up():
    session = get_session_factory()()
    try:
        outcome = run_multi_agent(session, uuid.uuid4())  # no such audit: both crews say so
    finally:
        session.close()

    described = outcome.as_dict()
    assert set(described["agents"]) == {"security", "architecture"}
    assert described["calls"] == outcome.calls
    assert all(error.startswith(("security:", "architecture:")) for error in described["errors"])


def _client_for(session, audit_id):
    from app.llm.client import LLMClient

    return LLMClient(session, audit_id, settings=settings_for())
