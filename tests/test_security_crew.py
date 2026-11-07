"""Step 2.8: the security agent, as a CrewAI crew.

The crew's model is replaced by a fake crew object, so these tests exercise the
parts that are ours — the prompt, the tools, the triage bookkeeping, and the
validation gate that decides what is allowed near the finding table — without a
key, a network call, or a ten-minute ReAct loop. What CrewAI does with a real
model is measured by the harness, not here.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agents import STAGES
from app.agents.prompts import load_prompt
from app.agents.security import (
    SOURCE_TOOL,
    GatewayLLM,
    _gateway_tools,
    _messages_for_gateway,
    _parse_verdicts,
    _security_tools,
    build_crew,
    run_security_agent,
)
from app.agents.tools import RepositoryTools
from app.core.config import Settings
from app.db.enums import (
    AuditStatus,
    Category,
    EvalConfig,
    EvidenceKind,
    FindingOrigin,
    Severity,
    SourceType,
    VerificationStatus,
    Verdict,
)
from app.db.models import Audit, Evidence, Finding, Repository
from app.db.session import get_session_factory
from app.indexing.service import index_audit, index_graph
from app.llm.client import LLMClient
from app.intake.filtering import index_snapshot
from app.retrieval.embeddings import HashingEmbeddingProvider, embed_chunks

REVIEWED_FILES = {
    "app/auth.py": '''"""Authentication helpers."""


def get_current_user(token):
    if not token:
        raise PermissionError("missing token")
    return {"user_id": 1, "owner_id": 7}
''',
    "app/routes/orders.py": '''"""Order routes."""

from app.store import get_order


def read_order(conn, order_id):
    """Return one order."""
    return get_order(conn, order_id)
''',
    "app/store.py": '''"""Data access."""


def get_order(conn, order_id):
    """Return one order by id."""
    return conn.execute("SELECT id FROM orders WHERE id = ?", (order_id,)).fetchone()
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


@dataclass
class FakeCrew:
    """A crew whose two tasks answer with prepared text."""

    triage: str = '{"verdicts": []}'
    investigation: str = '{"findings": []}'
    error: Exception | None = None
    seen: list = field(default_factory=list)

    def kickoff(self):
        """Answer as CrewAI would: one output per task, in order."""
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            tasks_output=[
                SimpleNamespace(raw=self.triage),
                SimpleNamespace(raw=self.investigation),
            ]
        )


def crew_factory_for(crew: FakeCrew):
    """A factory that records what the crew was handed and returns the fake."""

    def factory(tools, llm, findings):
        crew.seen.append({"tools": tools, "llm": llm, "findings": findings})
        return crew

    return factory


def reply(*findings: dict) -> str:
    """A model reply containing exactly these findings."""
    return json.dumps({"findings": list(findings)})


def authz_finding(**overrides) -> dict:
    base = {
        "category": "security",
        "title": "Order route reads any order without checking the caller",
        "file": "app/routes/orders.py",
        "line_start": 6,
        "line_end": 8,
        "severity": "high",
        "confidence": 0.9,
        "reasoning": "app/auth.py defines get_current_user and this route never calls it.",
        "recommendation": "Resolve the caller and scope the query to its owner.",
    }
    base.update(overrides)
    return base


def add_finding(
    session,
    audit_id,
    *,
    category: Category = Category.SECURITY,
    title: str = "Request handler reaches a row without an ownership check",
    path: str = "app/routes/orders.py",
) -> uuid.UUID:
    """Seed one scanner finding, the way the scanner stage would."""
    row = Finding(
        audit_id=audit_id,
        category=category,
        title=title,
        severity=Severity.HIGH,
        confidence=Decimal("0.80"),
        origin=FindingOrigin.SCANNER,
        source_tool="semgrep",
        verification_status=VerificationStatus.SCANNER_CONFIRMED,
        verdict=Verdict.ISSUE,
        reasoning="the scanner flagged this call site",
        fingerprint=uuid.uuid4().hex,
    )
    session.add(row)
    session.flush()
    session.add(
        Evidence(
            finding_id=row.id,
            file_path=path,
            line_start=6,
            line_end=8,
            snippet="    return get_order(conn, order_id)",
            kind=EvidenceKind.PRIMARY,
        )
    )
    session.commit()
    return row.id


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory) -> Path:
    """One small repository on disk, written once for the whole module."""
    root = tmp_path_factory.mktemp("crew-repo")
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
        name=f"pytest-crew-{uuid.uuid4().hex[:8]}",
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


def run(audit_id, crew: FakeCrew):
    """Run the crew stage over the audit with a scripted crew, then close."""
    session = get_session_factory()()
    client = LLMClient(session, audit_id, settings=settings_for())
    outcome = run_security_agent(
        session, audit_id, client=client, crew_factory=crew_factory_for(crew)
    )
    return outcome, session


def findings_of(session, audit_id) -> list[Finding]:
    return list(session.execute(select(Finding).where(Finding.audit_id == audit_id)).scalars())


# --------------------------------------------------------------------------- #
# what the stage is
# --------------------------------------------------------------------------- #


def test_the_stage_is_registered_as_the_multi_agent_configuration():
    assert STAGES[EvalConfig.MULTI_AGENT] is run_security_agent


def test_the_prompt_has_every_section_the_crew_reads():
    prompt = load_prompt("security")

    assert {"role", "goal", "backstory", "triage", "investigation", "json_contract"} <= set(prompt)
    assert "{{findings}}" in prompt["triage"]
    assert "json" in prompt["json_contract"].lower()


def test_the_prompt_never_claims_retrieved_text_is_trusted():
    """An injected instruction in a repository file must not be able to obey it."""
    prompt = load_prompt("security")
    combined = " ".join(prompt.values()).lower()

    assert "untrusted" in combined
    assert "data" in combined


# --------------------------------------------------------------------------- #
# what it is allowed to touch
# --------------------------------------------------------------------------- #


def test_the_crew_gets_exactly_the_three_read_only_tools(audit):
    tools = RepositoryTools(get_session_factory()(), audit)
    named = {tool.name: tool for tool in _security_tools(tools)}

    assert set(named) == {"retrieve_context", "get_symbol", "read_finding"}
    # Nothing here writes, so a confused model has no write path to reach.
    assert not any(hasattr(tool, "write") for tool in named.values())


def test_retrieval_returns_delimited_repository_text(audit):
    session = get_session_factory()()
    named = {tool.name: tool for tool in _security_tools(RepositoryTools(session, audit))}

    text = named["retrieve_context"].run(query="order ownership check")

    assert "<<<DATA" in text and "<<<END_DATA>>>" in text
    session.close()


def test_a_symbol_lookup_renders_code_in_the_same_delimiters(audit):
    session = get_session_factory()()
    named = {tool.name: tool for tool in _security_tools(RepositoryTools(session, audit))}

    text = named["get_symbol"].run(name="read_order")

    assert "read_order" in text
    assert "<<<DATA" in text
    session.close()


def test_reading_a_finding_by_bad_id_answers_instead_of_raising(audit):
    session = get_session_factory()()
    named = {tool.name: tool for tool in _security_tools(RepositoryTools(session, audit))}

    assert "not a finding id" in named["read_finding"].run(finding_id="not-a-uuid")
    session.close()


# --------------------------------------------------------------------------- #
# task 1: triage
# --------------------------------------------------------------------------- #


def test_only_security_and_secret_findings_are_handed_to_the_crew(audit):
    session = get_session_factory()()
    security_id = add_finding(session, audit)
    add_finding(session, audit, category=Category.ARCHITECTURE, title="Import cycle between modules")
    session.close()

    crew = FakeCrew()
    outcome, session = run(audit, crew)
    try:
        triaged = crew.seen[0]["findings"]
        assert [view.finding_id for view in triaged] == [security_id]
        assert outcome.triaged == 0  # the model returned no verdicts
    finally:
        session.close()


def test_a_triage_verdict_cites_the_finding_it_judged(audit):
    session = get_session_factory()()
    finding_id = add_finding(session, audit)
    session.close()

    crew = FakeCrew(
        triage=json.dumps(
            {
                "verdicts": [
                    {
                        "finding_id": str(finding_id),
                        "verdict": "likely_real",
                        "reasoning": "app/routes/orders.py:6-8 calls get_order with a client-supplied id",
                    },
                    {
                        "finding_id": str(uuid.uuid4()),
                        "verdict": "likely_false_positive",
                        "reasoning": "the guard is in the decorator",
                    },
                    {"finding_id": str(uuid.uuid4()), "verdict": "unclear", "reasoning": "?"},
                ]
            }
        )
    )
    outcome, session = run(audit, crew)
    try:
        assert outcome.triaged == 3
        assert outcome.likely_real == 1
        assert outcome.likely_false_positive == 1
        assert outcome.uncertain == 1  # an unknown verdict is not silently dropped
        assert outcome.verdicts[0].finding_id == str(finding_id)
        assert outcome.verdicts[0].reasoning.startswith("app/routes/orders.py:6-8")
    finally:
        session.close()


def test_triage_verdicts_do_not_overwrite_the_scanner_row(audit):
    """The harness scores the scanners on their own rows; triage is reporting."""
    session = get_session_factory()()
    finding_id = add_finding(session, audit)
    session.close()

    crew = FakeCrew(
        triage=json.dumps(
            {
                "verdicts": [
                    {
                        "finding_id": str(finding_id),
                        "verdict": "likely_false_positive",
                        "reasoning": "the caller is authenticated upstream",
                    }
                ]
            }
        )
    )
    outcome, session = run(audit, crew)
    try:
        row = session.get(Finding, uuid.UUID(str(finding_id)))
        assert row.verification_status is VerificationStatus.SCANNER_CONFIRMED
        assert outcome.likely_false_positive == 1
    finally:
        session.close()


def test_unparseable_triage_is_reported_rather_than_counted():
    assert _parse_verdicts("I could not decide.") == ()


def test_a_fenced_json_triage_answer_is_still_read():
    verdicts = _parse_verdicts(
        'Here you go:\n```json\n{"verdicts": [{"verdict": "likely_real", "reasoning": "x"}]}\n```'
    )

    assert verdicts[0].verdict == "likely_real"


# --------------------------------------------------------------------------- #
# task 2: new findings, through the validator
# --------------------------------------------------------------------------- #


def test_an_investigated_finding_is_stored_as_an_ai_suggestion(audit):
    crew = FakeCrew(investigation=reply(authz_finding()))
    outcome, session = run(audit, crew)
    try:
        assert outcome.errors == ()
        assert outcome.accepted == 1
        assert outcome.rejected == 0

        row = next(f for f in findings_of(session, audit) if f.source_tool == SOURCE_TOOL)
        assert row.origin is FindingOrigin.AGENT
        assert row.verification_status is VerificationStatus.AI_SUGGESTED
        assert row.category is Category.SECURITY
    finally:
        session.close()


def test_a_finding_without_a_real_file_and_line_range_is_rejected(audit):
    """The gate is the requirement: an unciteable finding never reaches the table."""
    crew = FakeCrew(
        investigation=reply(
            authz_finding(),
            authz_finding(title="Every request is unauthenticated", file="/etc/passwd"),
            authz_finding(title="Ownership check missing in store", file="app/store.py", line_start=900, line_end=905),
        )
    )
    outcome, session = run(audit, crew)
    try:
        assert outcome.accepted == 1
        assert outcome.rejected == 2
        stored_paths = {
            evidence.file_path
            for row in findings_of(session, audit)
            for evidence in row.evidence
        }
        assert "/etc/passwd" not in stored_paths
        assert stored_paths == {"app/routes/orders.py"}
    finally:
        session.close()


def test_an_answer_with_nothing_parseable_is_reported_not_raised(audit):
    crew = FakeCrew(investigation="I found the gap but will not format it as JSON.")
    outcome, session = run(audit, crew)
    try:
        assert outcome.errors == ()
        assert outcome.dropped == 1
        assert any("unparseable" in note for note in outcome.notes)
        assert findings_of(session, audit) == []
    finally:
        session.close()


def test_a_crew_that_raises_leaves_the_audit_intact(audit):
    crew = FakeCrew(error=RuntimeError("provider went away"))
    outcome, session = run(audit, crew)
    try:
        assert outcome.errors
        assert "RuntimeError" in outcome.errors[0]
        assert findings_of(session, audit) == []
    finally:
        session.close()


def test_the_outcome_is_json_ready_for_the_eval_record(audit):
    crew = FakeCrew(investigation=reply(authz_finding()))
    outcome, session = run(audit, crew)
    try:
        payload = outcome.as_dict()
        assert isinstance(payload["notes"], list)
        assert payload["triaged"] == 0
        assert payload["accepted"] == 1
        assert json.dumps(payload)  # no Decimal or UUID leaked in
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# the crew definition itself
# --------------------------------------------------------------------------- #


class FakeClient:
    """The minimum of ``LLMClient`` the adapter reads, with no network path."""

    settings = SimpleNamespace(llm_default_model="gpt-4o-mini")

    def complete(self, *args, **kwargs):
        raise AssertionError("no model call in a crew-construction test")


@dataclass
class ScriptedToolClient:
    """A client that asks for one tool per task and then answers."""

    replies: list
    calls: list = field(default_factory=list)
    settings: SimpleNamespace = field(
        default_factory=lambda: SimpleNamespace(llm_default_model="gpt-4o-mini")
    )

    def complete(self, agent, messages, max_tokens=None, tools=None):
        self.calls.append({"messages": messages, "tools": tools})
        reply = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        if isinstance(reply, dict):
            return SimpleNamespace(text="", tool_calls=(reply,))
        return SimpleNamespace(text=reply, tool_calls=())


def test_a_tool_call_is_returned_as_a_call_and_the_schemas_are_forwarded():
    """The model asks for a tool; the adapter must not turn that into an answer."""
    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "retrieve_context", "arguments": '{"query": "read_order auth"}'},
    }
    client = ScriptedToolClient(replies=[tool_call, '{"findings": []}'])
    llm = GatewayLLM(client)

    answer = llm.call(
        messages=[{"role": "user", "content": "find the authorization gap"}],
        tools=[{"type": "function", "function": {"name": "retrieve_context", "parameters": {}}}],
    )

    assert answer[0]["function"]["name"] == "retrieve_context"
    assert client.calls[0]["tools"][0]["function"]["name"] == "retrieve_context"
    assert llm.calls == 1  # still one budgeted call, not one per tool


def test_tool_schemas_lose_the_strict_flag_the_provider_rejects():
    """CrewAI sets strict: true; Cohere answers 422 and the crew never starts."""
    tools = _gateway_tools(
        [{"type": "function", "function": {"name": "retrieve_context", "parameters": {}, "strict": True}}]
    )

    assert "strict" not in tools[0]["function"]
    assert tools[0]["function"]["name"] == "retrieve_context"
    assert _gateway_tools(None) is None


def test_a_tool_result_keeps_the_call_it_answers():
    """A tool result without its call id is a message no provider can match."""
    payload = _messages_for_gateway(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "function": {"name": "f", "arguments": "{}"}}],
            },
            {"role": "tool", "content": "<<<DATA>>>", "tool_call_id": "call_1"},
        ]
    )

    assert payload[0]["content"] == ""  # null content is still a message
    assert payload[0]["tool_calls"][0]["function"]["name"] == "f"
    assert payload[1]["tool_call_id"] == "call_1"


def test_the_investigation_guardrail_refuses_an_answer_written_without_reading(audit):
    """A crew that answered from memory once answered with the scanner's own finding."""
    session = get_session_factory()()
    tools = RepositoryTools(session, audit)
    crew = build_crew(tools, GatewayLLM(FakeClient()), ())
    triage, investigate = crew.tasks
    try:
        passed, feedback = investigate.guardrail("{}")
        assert passed is False
        assert "retrieve_context" in feedback

        crew.agents[0].tools[0].run(query="order route")  # a real search
        passed, feedback = investigate.guardrail("{}")
        assert passed is False  # searched, but never compared the auth helper
        assert "get_symbol" in feedback

        crew.agents[0].tools[1].run(name="get_current_user")
        passed, echoed = investigate.guardrail("{}")
        assert passed is True
        assert echoed == "{}"

        triage.callback("{}")  # the second task starts with an empty log
        passed, _ = investigate.guardrail("{}")
        assert passed is False
    finally:
        session.close()


def test_the_crew_is_one_agent_with_two_sequential_tasks(audit):
    session = get_session_factory()()
    tools = RepositoryTools(session, audit)
    crew = build_crew(tools, GatewayLLM(FakeClient()), ())
    try:
        assert len(crew.agents) == 1
        assert [task.description for task in crew.tasks]  # both prompts rendered
        # Handing the triage transcript to the second task made the model
        # restate the scanner findings it had just triaged instead of hunting
        # for what no scanner sees, so the two tasks stay independent.
        assert crew.tasks[1].context != [crew.tasks[0]]
        assert crew.agents[0].llm.supports_function_calling() is True
    finally:
        session.close()
