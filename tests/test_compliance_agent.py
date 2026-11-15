"""Step 3.1: the compliance agent, against a real indexed snapshot.

The crew's model is replaced by a scripted answer, so what these tests exercise is
the part that is ours: what the crew is given (the ScanCode inventory, the OSV
advisories, the licence policy, and the gaps between them), what it is allowed to
store, and the rule the stage exists for - missing licence data answers
``insufficient_evidence`` and never ``no_issue``.
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

from app.agents.compliance import (
    AGENT_ROLE,
    SOURCE_TOOL,
    ComplianceFacts,
    cites_licence_evidence,
    read_facts,
    run_compliance_agent,
    validate_compliance_report,
)
from app.agents.crew import GatewayLLM
from app.agents.multi import MultiAgentOutcome
from app.agents.prompts import load_prompt
from app.agents.schema import AgentReport
from app.agents.tools import BLOCK_CLOSE, BLOCK_OPEN, RepositoryTools
from app.db.enums import (
    AuditStatus,
    Category,
    EvidenceKind,
    FindingOrigin,
    Severity,
    SourceType,
    Verdict,
    VerificationStatus,
)
from app.db.models import Audit, Evidence, Finding, Repository
from app.db.session import get_session_factory
from app.indexing.service import index_audit, index_graph
from app.intake.filtering import index_snapshot
from app.retrieval.embeddings import HashingEmbeddingProvider, embed_chunks

FILES = {
    "requirements.txt": "jinja2==2.11.2\nmysqlclient==2.2.0\n",
    "LICENSE": "MIT License\n\nCopyright (c) 2024 Example Ltd\n",
    "app/main.py": '''"""Application entry point."""


def main() -> None:
    print("hello")
''',
}


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("compliance-repo")
    for path, content in FILES.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


@pytest.fixture
def audit(snapshot):
    """A fresh indexed audit over the repository, removed afterwards."""
    session = get_session_factory()()
    repository = Repository(
        name=f"pytest-compliance-{uuid.uuid4().hex[:8]}",
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
    audit_id = audit.id

    index_snapshot(session, audit_id, snapshot)
    index_audit(session, audit_id, snapshot_root=snapshot)
    index_graph(session, audit_id, snapshot_root=snapshot)
    embed_chunks(session, audit_id, provider=HashingEmbeddingProvider(), batch_size=8)
    session.close()

    yield audit_id

    session = get_session_factory()()
    session.delete(session.get(Audit, audit_id))
    session.commit()
    session.delete(session.get(Repository, repository.id))
    session.commit()
    session.close()


def add_license_finding(
    audit_id,
    *,
    path: str = "requirements.txt",
    line: int = 2,
    title: str = "mysqlclient 2.2.0 is GPL-2.0-only (GPL)",
    snippet: str = "mysqlclient==2.2.0",
    rule_id: str = "license-policy:GPL",
    verdict: Verdict = Verdict.ISSUE,
    severity: Severity = Severity.MEDIUM,
) -> uuid.UUID:
    """One licence finding, as ScanCode stores it."""
    session = get_session_factory()()
    finding = Finding(
        audit_id=audit_id,
        category=Category.LICENSE,
        title=title,
        severity=severity,
        confidence=Decimal("0.80"),
        origin=FindingOrigin.SCANNER,
        source_tool=rule_id,
        verification_status=VerificationStatus.SCANNER_CONFIRMED,
        verdict=verdict,
        reasoning="pypi publishes the license for mysqlclient 2.2.0 as 'GPL-2.0-only'",
        fingerprint=uuid.uuid4().hex,
    )
    session.add(finding)
    session.flush()
    session.add(
        Evidence(
            finding_id=finding.id,
            file_path=path,
            line_start=line,
            line_end=line,
            snippet=snippet,
            kind=EvidenceKind.PRIMARY,
        )
    )
    session.commit()
    finding_id = finding.id
    session.close()
    return finding_id


class SilentClient:
    """The minimum ``LLMClient`` the adapter reads; the scripted crew never calls it."""

    settings = SimpleNamespace(llm_default_model="gpt-4o-mini")

    def complete(self, *args, **kwargs):
        raise AssertionError("a scripted crew must not reach the model")


@dataclass
class ScriptedCrew:
    """One task's answer, in the shape ``kickoff`` returns it."""

    answer: str
    seen: list = field(default_factory=list)

    def kickoff(self):
        return SimpleNamespace(tasks_output=[SimpleNamespace(raw=self.answer)])


def crew_factory_for(crew: ScriptedCrew):
    def factory(tools, llm, facts):
        crew.seen.append(facts)
        return crew

    return factory


def answer(*findings: dict) -> str:
    return json.dumps({"findings": list(findings)})


def license_finding(**overrides) -> dict:
    """The planted restrictive dependency, as the crew reports it."""
    base = {
        "category": "license",
        "title": "mysqlclient 2.2.0 is GPL-2.0-only, so shipping it obliges source release",
        "file": "requirements.txt",
        "line_start": 2,
        "line_end": 2,
        "severity": "medium",
        "confidence": 0.9,
        "verdict": "issue",
        "reasoning": (
            "requirements.txt declares mysqlclient 2.2.0, whose licence is GPL-2.0-only."
            " A binary linked against it must be distributed under the same terms, which"
            " conflicts with shipping this service closed-source."
        ),
        "recommendation": "Replace mysqlclient with a permissive driver, or obtain a commercial licence.",
    }
    base.update(overrides)
    return base


def run(audit_id, crew: ScriptedCrew):
    """Run the stage over the audit with a scripted crew, then close."""
    session = get_session_factory()()
    outcome = run_compliance_agent(
        session,
        audit_id,
        client=SilentClient(),
        crew_factory=crew_factory_for(crew),
    )
    return outcome, session


def stored_findings(session, audit_id) -> list[Finding]:
    return list(
        session.execute(
            select(Finding).where(
                Finding.audit_id == audit_id, Finding.source_tool == SOURCE_TOOL
            )
        ).scalars()
    )


# --------------------------------------------------------------------------- #
# what the stage is
# --------------------------------------------------------------------------- #


def test_the_compliance_crew_is_part_of_the_composed_stage():
    assert "compliance" in MultiAgentOutcome.__dataclass_fields__
    assert "compliance" in MultiAgentOutcome().as_dict()["agents"]


def test_the_prompt_has_every_section_the_crew_reads():
    prompt = load_prompt("compliance")

    assert {"role", "goal", "backstory", "review", "json_contract"} <= set(prompt)
    assert "{{facts}}" in prompt["review"]
    assert "insufficient_evidence" in prompt["review"]


def test_the_prompt_tells_the_crew_that_missing_data_is_not_compliance():
    prompt = load_prompt("compliance")
    combined = " ".join(" ".join(prompt.values()).lower().split())

    assert "never report a licence as fine because nothing was found" in combined
    assert "no_issue" in combined and "insufficient_evidence" in combined


def test_the_facts_put_the_inventory_and_the_policy_in_the_prompt(audit):
    add_license_finding(audit)
    session = get_session_factory()()
    try:
        tools = RepositoryTools(session, audit)
        facts = read_facts(session, audit, tools)
    finally:
        session.close()

    rendered = facts.render()

    assert "mysqlclient 2.2.0 is GPL-2.0-only" in rendered
    assert "restrictive:" in rendered and "GPL" in rendered
    assert "requirements.txt:2-2" in rendered
    assert facts.inventory_incomplete is False


def test_the_crew_holds_only_the_read_only_tools(audit):
    from app.agents.compliance import build_crew

    session = get_session_factory()()
    try:
        tools = RepositoryTools(session, audit)
        facts = read_facts(session, audit, tools)
        crew = build_crew(tools, GatewayLLM(SilentClient(), agent=AGENT_ROLE), facts)
        names = {tool.name for tool in crew.agents[0].tools}
    finally:
        session.close()

    assert names == {"retrieve_context", "get_symbol", "read_finding"}


# --------------------------------------------------------------------------- #
# test 1: the planted restrictive licence is flagged, with evidence
# --------------------------------------------------------------------------- #


def test_the_planted_restrictive_license_is_flagged_with_evidence(audit):
    add_license_finding(audit)
    outcome, session = run(audit, ScriptedCrew(answer(license_finding())))
    try:
        rows = stored_findings(session, audit)
        evidence = [
            (item.file_path, item.line_start, item.line_end, item.snippet)
            for row in rows
            for item in row.evidence
        ]
    finally:
        session.close()

    assert outcome.errors == ()
    assert outcome.accepted == 1
    assert len(rows) == 1
    row = rows[0]
    assert row.category is Category.LICENSE
    assert row.verdict is Verdict.ISSUE
    assert row.origin is FindingOrigin.AGENT
    assert row.verification_status is VerificationStatus.AI_SUGGESTED
    assert "GPL" in row.title

    # The citation is the manifest line that declares the dependency.
    assert evidence == [("requirements.txt", 2, 2, "mysqlclient==2.2.0")]


def test_a_finding_that_cites_source_code_is_rejected(audit):
    add_license_finding(audit)
    crew = ScriptedCrew(answer(license_finding(file="app/main.py", line_start=4, line_end=5)))
    outcome, session = run(audit, crew)
    try:
        rows = stored_findings(session, audit)
    finally:
        session.close()

    assert outcome.accepted == 0
    assert outcome.rejected == 1
    assert rows == []


@pytest.mark.parametrize(
    "path, expected",
    [
        ("requirements.txt", True),
        ("requirements-dev.txt", True),
        ("backend/requirements.txt", True),
        ("package.json", True),
        ("pyproject.toml", True),
        ("LICENSE", True),
        ("LICENSE.md", True),
        ("COPYING", True),
        ("NOTICE", True),
        ("app/main.py", False),
        ("docs/readme.txt", False),
    ],
)
def test_only_a_manifest_or_a_license_file_counts_as_evidence(path, expected):
    assert cites_licence_evidence(path) is expected


# --------------------------------------------------------------------------- #
# test 2: missing licence data answers insufficient_evidence, never no_issue
# --------------------------------------------------------------------------- #


def test_missing_license_data_downgrades_a_no_issue_answer(audit):
    """No licence finding at all: the inventory cannot support "no issue"."""
    crew = ScriptedCrew(
        answer(
            license_finding(
                title="mysqlclient 2.2.0 is permissively licensed and safe to ship",
                verdict="no_issue",
                line_start=1,
                line_end=1,
            )
        )
    )
    outcome, session = run(audit, crew)
    try:
        rows = stored_findings(session, audit)
        verdicts = [row.verdict for row in rows]
        reasoning = rows[0].reasoning if rows else ""
    finally:
        session.close()

    assert outcome.accepted == 1
    assert verdicts == [Verdict.INSUFFICIENT_EVIDENCE]
    assert "downgraded to insufficient_evidence" in reasoning
    assert any("downgraded" in note for note in outcome.notes)


def test_an_incomplete_inventory_with_no_answer_still_reports_the_gaps(audit):
    """A crew that says nothing must not leave the audit looking licence-clean."""
    outcome, session = run(audit, ScriptedCrew(answer()))
    try:
        rows = stored_findings(session, audit)
        evidence = [item.file_path for row in rows for item in row.evidence]
        verdicts = [row.verdict for row in rows]
    finally:
        session.close()

    assert outcome.accepted == 1
    assert verdicts == [Verdict.INSUFFICIENT_EVIDENCE]
    assert evidence == ["requirements.txt"]
    assert outcome.gaps  # the facts said why


def test_a_dependency_with_no_license_data_is_a_gap(audit):
    add_license_finding(
        audit,
        title="No license data for mysqlclient 2.2.0",
        rule_id="license-policy:missing",
        verdict=Verdict.INSUFFICIENT_EVIDENCE,
        severity=Severity.LOW,
    )
    session = get_session_factory()()
    try:
        facts = read_facts(session, audit, RepositoryTools(session, audit))
    finally:
        session.close()

    assert facts.inventory_incomplete is True
    assert any("mysqlclient" in gap for gap in facts.gaps)


def test_the_guardrail_refuses_no_issue_while_the_inventory_has_gaps(audit):
    from app.agents.compliance import build_crew

    session = get_session_factory()()
    try:
        tools = RepositoryTools(session, audit)
        incomplete = ComplianceFacts(gaps=("the licence inventory is empty",))
        complete = ComplianceFacts(gaps=())
        refused = build_crew(tools, GatewayLLM(SilentClient(), agent=AGENT_ROLE), incomplete)
        allowed = build_crew(tools, GatewayLLM(SilentClient(), agent=AGENT_ROLE), complete)

        passed, feedback = refused.tasks[0].guardrail(answer(license_finding(verdict="no_issue")))
        assert passed is False
        assert "insufficient_evidence" in feedback

        passed, _ = allowed.tasks[0].guardrail(answer(license_finding(verdict="no_issue")))
        assert passed is True
    finally:
        session.close()


def test_the_guardrail_refuses_a_finding_that_cites_source_code(audit):
    from app.agents.compliance import build_crew

    session = get_session_factory()()
    try:
        tools = RepositoryTools(session, audit)
        crew = build_crew(tools, GatewayLLM(SilentClient(), agent=AGENT_ROLE), ComplianceFacts())
        passed, feedback = crew.tasks[0].guardrail(answer(license_finding(file="app/main.py")))
    finally:
        session.close()

    assert passed is False
    assert "manifest" in feedback


def test_a_complete_inventory_keeps_a_no_issue_verdict_out_of_the_finding_table(audit):
    """no_issue is a verdict about the inventory, not a problem to store."""
    add_license_finding(audit)
    crew = ScriptedCrew(
        answer(
            license_finding(file="LICENSE", line_start=1, line_end=1, verdict="no_issue"),
            license_finding(),
        )
    )
    session = get_session_factory()()
    try:
        tools = RepositoryTools(session, audit)
        facts = read_facts(session, audit, tools)
        outcome = run_compliance_agent(
            session, audit, client=SilentClient(), crew_factory=crew_factory_for(crew)
        )
        rows = stored_findings(session, audit)
        verdicts = [row.verdict for row in rows]
    finally:
        session.close()

    assert facts.inventory_incomplete is False  # the premise of the test
    assert outcome.cleared == 1
    assert verdicts == [Verdict.ISSUE]


def test_validate_keeps_the_floor_when_the_inventory_is_incomplete(audit):
    session = get_session_factory()()
    try:
        tools = RepositoryTools(session, audit)
        complete = ComplianceFacts(gaps=())
        report = AgentReport.model_validate(
            {"findings": [license_finding(verdict="no_issue", line_start=1, line_end=1)]}
        )
        kept, rewritten = validate_compliance_report(report, tools, complete)
        forced, forced_count = validate_compliance_report(
            report, tools, ComplianceFacts(gaps=("inventory is empty",))
        )
    finally:
        session.close()

    assert rewritten == 0
    assert kept.accepted[0].verdict == "no_issue"
    assert forced_count == 1
    assert forced.accepted[0].verdict == "insufficient_evidence"


def test_the_outcome_is_json_ready(audit):
    add_license_finding(audit)
    outcome, session = run(audit, ScriptedCrew(answer(license_finding())))
    try:
        payload = outcome.as_dict()
    finally:
        session.close()

    assert payload["licenses"] == 1
    assert payload["dependencies"] == 0
    assert payload["verdicts"]["issue"] == 1
    assert isinstance(payload["notes"], list)
    assert json.dumps(payload)  # no Decimal or UUID leaked in
