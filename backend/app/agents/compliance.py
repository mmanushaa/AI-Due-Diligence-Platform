"""The compliance agent, as a CrewAI crew (step 3.1).

The scanners answer "which licence is this?" and nothing more. ScanCode says a
dependency is GPL-2.0; it cannot say what that obliges a company that ships the
product to do. This crew reads the inventory the scanners produced and turns it
into plain-language obligations and distribution conflicts:

    licenses      every licence finding ScanCode stored, with the rule and the
                  file it came from (``category='license'``)
    dependencies  every OSV advisory, because a dependency's licence and its
                  vulnerabilities are read by the same buyer
    policy        ``app/config/license_policy.json``: the restrictive families,
                  their severity and their obligations, and the permissive ones
    gaps          what the inventory does not cover: no licence data at all, a
                  dependency whose licence could not be established, an unreadable
                  policy file

The rule the stage exists to enforce is about the *negative* answer. A model with
half an inventory will still answer "no issue here" if it is allowed to, and a
report that says a licence is fine because nothing was found is worse than no
report. So an answer of ``no_issue`` while the inventory has a gap is rewritten to
``insufficient_evidence`` before anything is stored, and if the crew reports
nothing at all while the inventory is incomplete, one ``insufficient_evidence``
finding is written that cites the manifest. Missing data is never compliance.

Every finding has to cite a manifest (``requirements.txt``, ``package.json``, …) or
a licence file (``LICENSE``, ``COPYING``, …): those are the two places a licence
claim can actually be checked against. A finding that cites source code is rejected
and counted, because "the code looks like it is GPL" is not a licence citation.

Only ``issue`` and ``insufficient_evidence`` findings are stored. ``no_issue`` is a
verdict about the inventory, not a problem in it, and writing it to the finding
table would make the benchmark score an absence as a report.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.agents.baseline_single import AgentOutcome
from app.agents.crew import GatewayLLM, crew_llm, read_only_tools
from app.agents.events import record_final_output, record_guardrail_failure
from app.agents.prompts import fill, load_prompt
from app.agents.prompt_builder import build_prompt
from app.agents.schema import (
    AgentFinding,
    AgentReport,
    InvalidOutput,
    RejectedFinding,
    ValidationResult,
    parse_crew_report,
    validate_report,
)
from app.agents.store import ensure_index, store_findings
from app.agents.tools import RepositoryTools
from app.db.enums import AgentName, Category, Verdict
from app.db.models import Finding
from app.llm.client import LLMClient, LLMError
from app.scanners.scancode import POLICY_PATH, LicensePolicy, load_policy

logger = logging.getLogger(__name__)

SOURCE_TOOL = "compliance_agent"
AGENT_ROLE = AgentName.COMPLIANCE
MAX_ITERATIONS = 6
#: Guardrail retries after a refused answer, shared with the other crews.
GUARDRAIL_RETRIES = 2
#: How much of the inventory one prompt carries. Beyond this the licence problem
#: is already visible in the numbers, and the model does not need every row.
MAX_LICENSE_FACTS = 40
MAX_DEPENDENCY_FACTS = 20

#: The files a licence claim can be checked against: a manifest that declares a
#: dependency, or a licence/notice file shipped with the code.
MANIFEST_NAMES = frozenset(
    {
        "requirements.txt",
        "requirements-dev.txt",
        "requirements-prod.txt",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "pipfile",
        "pipfile.lock",
        "poetry.lock",
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "cargo.toml",
        "cargo.lock",
        "go.mod",
        "go.sum",
        "gemfile",
        "gemfile.lock",
        "composer.json",
        "composer.lock",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
    }
)
LICENSE_FILE = re.compile(
    r"^(?:licen[cs]e|copying|notice|unlicense|authors)(?:[._-].*)?$", re.IGNORECASE
)
MANIFEST_FILE = re.compile(r"^(?:requirements[._-].*|.*\.(?:toml|txt|json|lock|gradle|sum|mod))$")


def _name(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").split("/")[-1].lower()


def is_manifest(path: str) -> bool:
    """Whether a file declares dependencies, as opposed to shipping a licence."""
    name = _name(path)
    if name in MANIFEST_NAMES:
        return True
    # ``requirements-dev.txt`` and friends, but not every .txt in the repository.
    return bool(name.startswith("requirements") and MANIFEST_FILE.match(name))


def cites_licence_evidence(path: str) -> bool:
    """Whether a cited file is one a licence claim can be checked against."""
    return is_manifest(path) or bool(LICENSE_FILE.match(_name(path)))


@dataclass(frozen=True)
class InventoryFact:
    """One licence or dependency finding the scanners stored."""

    title: str
    category: str
    severity: str
    verdict: str
    rule_id: str
    file_path: str
    line_start: int
    line_end: int
    snippet: str

    def render(self) -> str:
        where = f"{self.file_path}:{self.line_start}-{self.line_end}"
        evidence = f'\n    evidence: "{self.snippet[:200]}"' if self.snippet else ""
        return (
            f"  - [{self.category}/{self.severity}/{self.verdict}] {self.title} "
            f"at {where} (rule {self.rule_id}){evidence}"
        )


@dataclass(frozen=True)
class PolicyFact:
    """One licence family from the policy file."""

    name: str
    restrictive: bool
    severity: str | None
    summary: str | None

    def render(self) -> str:
        if not self.restrictive:
            return self.name
        detail = f" ({self.severity})" if self.severity else ""
        obligation = f" -- {self.summary}" if self.summary else ""
        return f"{self.name}{detail}{obligation}"


@dataclass(frozen=True)
class ComplianceFacts:
    """Everything the crew is given, computed rather than guessed."""

    licenses: tuple[InventoryFact, ...] = ()
    dependencies: tuple[InventoryFact, ...] = ()
    restrictive: tuple[PolicyFact, ...] = ()
    permissive: tuple[PolicyFact, ...] = ()
    gaps: tuple[str, ...] = ()
    policy_path: str = ""

    @property
    def inventory_incomplete(self) -> bool:
        """True when a `no_issue` answer would be a claim the data cannot support."""
        return bool(self.gaps)

    def render(self) -> str:
        lines: list[str] = []
        lines.append(f"Licence inventory (ScanCode): {len(self.licenses)} finding(s)")
        lines.extend(fact.render() for fact in self.licenses)
        if not self.licenses:
            lines.append("  - none: no licence finding was stored for this repository")
        lines.append("")
        lines.append(f"Dependency advisories (OSV): {len(self.dependencies)} finding(s)")
        lines.extend(fact.render() for fact in self.dependencies)
        if not self.dependencies:
            lines.append("  - none: OSV stored no advisory for this repository")
        lines.append("")
        lines.append(f"Licence policy ({self.policy_path or 'unavailable'}):")
        lines.append(
            "  restrictive: "
            + ("; ".join(fact.render() for fact in self.restrictive) or "none listed")
        )
        lines.append(
            "  permissive: "
            + (", ".join(fact.render() for fact in self.permissive) or "none listed")
        )
        lines.append("")
        if self.gaps:
            lines.append(f"Gaps in the licence data ({len(self.gaps)}):")
            lines.extend(f"  - {gap}" for gap in self.gaps)
        else:
            lines.append("Gaps in the licence data: none")
        return "\n".join(lines)


def _inventory_facts(rows: list[Finding]) -> tuple[InventoryFact, ...]:
    facts: list[InventoryFact] = []
    for row in rows:
        evidence = next(iter(row.evidence), None)
        facts.append(
            InventoryFact(
                title=row.title,
                category=row.category.value,
                severity=row.severity.value,
                verdict=row.verdict.value,
                rule_id=row.source_tool or "unknown",
                file_path=evidence.file_path if evidence else "",
                line_start=evidence.line_start if evidence else 1,
                line_end=evidence.line_end if evidence else 1,
                snippet=(evidence.snippet or "") if evidence else "",
            )
        )
    return tuple(facts)


def _policy_facts(policy: LicensePolicy) -> tuple[tuple[PolicyFact, ...], tuple[PolicyFact, ...]]:
    restrictive = tuple(
        PolicyFact(
            name=family.name,
            restrictive=True,
            severity=family.severity.value if family.severity else None,
            summary=family.summary,
        )
        for family in policy.restrictive
    )
    permissive = tuple(
        PolicyFact(name=family.name, restrictive=False, severity=None, summary=family.summary)
        for family in policy.permissive
    )
    return restrictive, permissive


def read_facts(
    session: Session, audit_id: uuid.UUID, tools: RepositoryTools, *, policy_path=POLICY_PATH
) -> ComplianceFacts:
    """The licence inventory, the advisories and the policy, as data."""
    rows = list(
        session.execute(
            select(Finding)
            .where(
                Finding.audit_id == audit_id,
                Finding.category.in_((Category.LICENSE, Category.DEPENDENCY)),
            )
            .options(selectinload(Finding.evidence))
            .order_by(Finding.category, Finding.id)
        ).scalars()
    )
    licenses = _inventory_facts([row for row in rows if row.category is Category.LICENSE])[
        :MAX_LICENSE_FACTS
    ]
    dependencies = _inventory_facts(
        [row for row in rows if row.category is Category.DEPENDENCY]
    )[:MAX_DEPENDENCY_FACTS]

    gaps: list[str] = []
    restrictive: tuple[PolicyFact, ...] = ()
    permissive: tuple[PolicyFact, ...] = ()
    try:
        restrictive, permissive = _policy_facts(load_policy(policy_path))
    except (OSError, ValueError, KeyError) as exc:
        gaps.append(
            f"the licence policy file could not be read ({type(exc).__name__}), so no"
            " licence can be classified as permitted"
        )

    if not licenses:
        gaps.append(
            "the licence inventory is empty: ScanCode stored no licence finding, so"
            " neither the project's own licence nor its dependencies' licences are known"
        )
    for fact in licenses:
        if fact.verdict == Verdict.INSUFFICIENT_EVIDENCE.value:
            gaps.append(f"{fact.title} ({fact.file_path}:{fact.line_start})")
    if any(not fact.snippet for fact in licenses):
        gaps.append("at least one licence finding cites no readable line, so its text is unverified")

    return ComplianceFacts(
        licenses=licenses,
        dependencies=dependencies,
        restrictive=restrictive,
        permissive=permissive,
        gaps=tuple(gaps),
        policy_path=str(policy_path),
    )


def _refused(
    repository_tools: RepositoryTools, attempt: int, raw: Any, feedback: str
) -> tuple[bool, Any]:
    """Record the refused answer, then hand the refusal back to CrewAI."""
    record_guardrail_failure(
        repository_tools.session,
        repository_tools.audit_id,
        agent=AGENT_ROLE,
        attempt=attempt,
        raw_reply=str(raw),
        reason=feedback,
    )
    return False, feedback


def build_crew(
    repository_tools: RepositoryTools,
    llm: GatewayLLM,
    facts: ComplianceFacts,
) -> Any:
    """Assemble the one-agent, one-task compliance crew for this audit."""
    from crewai import Agent, Crew, Process, Task

    prompt = load_prompt("compliance")
    attempts: dict[str, int] = {}
    reviewer = Agent(
        role=prompt["role"],
        goal=prompt["goal"],
        backstory=prompt["backstory"],
        tools=read_only_tools(repository_tools, [], agent=AGENT_ROLE),
        llm=crew_llm(llm),
        allow_delegation=False,
        max_iter=MAX_ITERATIONS,
        verbose=False,
    )

    def answer_cites_licence_evidence(output):
        """Refuse an answer that would claim compliance the data cannot support."""
        raw = getattr(output, "raw", output) or ""
        attempts["review"] = attempts.get("review", 0) + 1
        attempt = attempts["review"]
        try:
            report = parse_crew_report(str(raw))
        except InvalidOutput:
            return _refused(
                repository_tools,
                attempt,
                raw,
                "That is not the JSON object from the contract. Reply with "
                '{"findings": [...]} and nothing else - no prose, no Markdown list.',
            )
        wrong_category = [item for item in report.findings if item.category != "license"]
        if wrong_category:
            return _refused(
                repository_tools,
                attempt,
                raw,
                "This stage reports licences only. Remove every finding whose category "
                "is not \"license\"; security, dependency and architecture problems "
                "belong to other stages.",
            )
        uncitable = [
            item for item in report.findings if not cites_licence_evidence(item.file)
        ]
        if uncitable:
            return _refused(
                repository_tools,
                attempt,
                raw,
                "A licence finding has to cite the manifest that declares the dependency "
                "(requirements.txt, package.json, pyproject.toml, ...) or the licence file "
                "itself (LICENSE, COPYING, NOTICE). Source files are not licence evidence. "
                f"Refused: {', '.join(item.file for item in uncitable[:3])}.",
            )
        if facts.inventory_incomplete and any(
            item.verdict == Verdict.NO_ISSUE.value for item in report.findings
        ):
            return _refused(
                repository_tools,
                attempt,
                raw,
                "The licence data for this repository is incomplete, so \"no_issue\" is "
                "not an answer you can give. Use \"insufficient_evidence\" for anything "
                "the inventory does not establish. The gaps are in the data block.",
            )
        return True, output

    review = Task(
        description=build_prompt(
            instructions=fill(
                prompt["review"],
                facts="(the licence inventory, the advisories and the policy are in the data block below)",
            ),
            facts=facts.render(),
        ),
        expected_output=prompt["json_contract"],
        agent=reviewer,
        guardrail=answer_cites_licence_evidence,
        guardrail_max_retries=GUARDRAIL_RETRIES,
    )
    return Crew(agents=[reviewer], tasks=[review], process=Process.sequential, verbose=False)


@dataclass(frozen=True)
class ComplianceOutcome(AgentOutcome):
    """The baseline's outcome plus what the inventory said."""

    licenses: int = 0
    dependencies: int = 0
    gaps: tuple[str, ...] = field(default_factory=tuple)
    verdicts: dict[str, int] = field(default_factory=dict)
    cleared: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        """JSON-friendly form for ``eval_runs.metrics``."""
        return {
            **super().as_dict(),
            "licenses": self.licenses,
            "dependencies": self.dependencies,
            "gaps": list(self.gaps),
            "verdicts": dict(self.verdicts),
            "cleared": self.cleared,
            "notes": list(self.notes),
        }


def validate_compliance_report(
    report: AgentReport, tools: RepositoryTools, facts: ComplianceFacts
) -> tuple[ValidationResult, int]:
    """Base validation, licence categories only, and the evidence floor.

    Returns the accepted findings and how many ``no_issue`` answers were rewritten
    because the inventory could not support them.
    """
    report = AgentReport(
        findings=[item for item in report.findings if item.category == "license"]
    )
    base = validate_report(report, tools)
    accepted: list[AgentFinding] = []
    rejected = list(base.rejected)
    rewritten = 0
    for finding in base.accepted:
        if not cites_licence_evidence(finding.file):
            rejected.append(
                RejectedFinding(finding.title, "not a manifest or a licence file")
            )
            continue
        if finding.verdict == Verdict.NO_ISSUE.value and facts.inventory_incomplete:
            rewritten += 1
            finding = finding.model_copy(
                update={
                    "verdict": Verdict.INSUFFICIENT_EVIDENCE.value,
                    "reasoning": (
                        "Reported as no_issue, but the licence inventory is incomplete, "
                        "so the answer is downgraded to insufficient_evidence. "
                        + finding.reasoning
                    )[:2_000],
                }
            )
        accepted.append(finding)
    return ValidationResult(accepted=tuple(accepted), rejected=tuple(rejected)), rewritten


def _first_manifest(tools: RepositoryTools, facts: ComplianceFacts) -> str | None:
    """A file to cite when the crew reported nothing.

    A manifest first: the gaps are about dependencies, and a manifest is where a
    dependency's licence is declared. A licence file only when there is no manifest.
    """
    for fact in facts.licenses:
        if is_manifest(fact.file_path):
            return fact.file_path
    known = sorted(tools.known_paths())
    for path in known:
        if is_manifest(path):
            return path
    for fact in facts.licenses:
        if cites_licence_evidence(fact.file_path):
            return fact.file_path
    for path in known:
        if cites_licence_evidence(path):
            return path
    return None


def _floor_finding(facts: ComplianceFacts, path: str) -> AgentFinding:
    """The insufficient_evidence finding written when the crew said nothing."""
    return AgentFinding(
        category="license",
        title=f"Licence obligations are unverified: {len(facts.gaps)} gap(s) in the inventory",
        file=path,
        line_start=1,
        line_end=1,
        severity="low",
        confidence=0.5,
        reasoning=(
            "No licence conclusion can be drawn from this inventory. Gaps: "
            + "; ".join(facts.gaps[:5])
        )[:2_000],
        recommendation=(
            "Establish the missing licence data (pin the dependency, obtain the licence "
            "from the maintainer or registry, or add the licence to the policy), then "
            "re-run the licence scan."
        ),
        verdict=Verdict.INSUFFICIENT_EVIDENCE.value,
    )


def run_compliance_agent(
    session: Session,
    audit_id: uuid.UUID,
    *,
    client: LLMClient | None = None,
    tools: RepositoryTools | None = None,
    crew_factory: Any = None,
    policy_path=POLICY_PATH,
) -> ComplianceOutcome:
    """Run the compliance crew over an indexed, scanned audit."""
    repository_tools = tools or RepositoryTools(session, audit_id)
    if not ensure_index(session, audit_id, repository_tools):
        return ComplianceOutcome(errors=("no readable snapshot to index",))

    facts = read_facts(session, audit_id, repository_tools, policy_path=policy_path)
    model_client = client or LLMClient(session, audit_id)
    llm = GatewayLLM(model_client, agent=AGENT_ROLE)
    started = time.monotonic()

    try:
        crew = (crew_factory or build_crew)(repository_tools, llm, facts)
        result = crew.kickoff()
    except LLMError as exc:
        logger.warning("compliance crew could not call the model for %s: %s", audit_id, exc)
        return ComplianceOutcome(
            calls=llm.calls,
            licenses=len(facts.licenses),
            dependencies=len(facts.dependencies),
            gaps=facts.gaps,
            errors=(str(exc),),
            notes=("crew aborted",),
        )
    except Exception as exc:  # noqa: BLE001 - a crew failure must not fail the audit
        logger.warning("compliance crew failed for %s: %s", audit_id, exc)
        return ComplianceOutcome(
            calls=llm.calls,
            licenses=len(facts.licenses),
            dependencies=len(facts.dependencies),
            gaps=facts.gaps,
            errors=(f"{type(exc).__name__}: {exc}"[:300],),
            notes=("crew aborted",),
        )

    latency_ms = int((time.monotonic() - started) * 1000)
    answer = _task_output(result, 0)
    record_final_output(session, audit_id, agent=AGENT_ROLE, task="review", raw=answer)
    logger.debug("compliance answer: %s", answer[:4_000])

    notes: list[str] = []
    try:
        report = parse_crew_report(answer)
    except InvalidOutput as exc:
        logger.info("compliance answer unparseable for %s: %s", audit_id, exc)
        return ComplianceOutcome(
            model=llm.model,
            calls=llm.calls,
            latency_ms=latency_ms,
            licenses=len(facts.licenses),
            dependencies=len(facts.dependencies),
            gaps=facts.gaps,
            rejected=1,
            dropped=1,
            notes=(f"answer unparseable: {exc}"[:200],),
        )

    validation, rewritten = validate_compliance_report(report, repository_tools, facts)
    counts = {verdict.value: 0 for verdict in Verdict}
    for finding in validation.accepted:
        counts[finding.verdict] = counts.get(finding.verdict, 0) + 1

    # A no_issue verdict is not a finding: storing it would score an absence.
    stored_candidates = tuple(
        finding
        for finding in validation.accepted
        if finding.verdict != Verdict.NO_ISSUE.value
    )
    cleared = len(validation.accepted) - len(stored_candidates)
    if rewritten:
        notes.append(f"{rewritten} no_issue answer(s) downgraded to insufficient_evidence")
    if cleared:
        notes.append(f"{cleared} no_issue verdict(s) recorded, not stored as findings")

    if not stored_candidates and facts.inventory_incomplete:
        path = _first_manifest(repository_tools, facts)
        if path is not None:
            stored_candidates = (_floor_finding(facts, path),)
            notes.append("no licence conclusion reported: wrote insufficient_evidence for the gaps")

    if validation.rejected:
        notes.append(f"{len(validation.rejected)} finding(s) rejected by the validator")
    stored = store_findings(
        session, audit_id, stored_candidates, repository_tools, source_tool=SOURCE_TOOL
    )
    if stored:
        notes.append(f"added {stored} new finding(s)")
    logger.info(
        "compliance crew for audit %s: %d licence finding(s), %d stored, %d gaps, %d calls",
        audit_id,
        len(facts.licenses),
        stored,
        len(facts.gaps),
        llm.calls,
    )
    return ComplianceOutcome(
        model=llm.model,
        calls=llm.calls,
        accepted=stored,
        rejected=len(validation.rejected),
        latency_ms=latency_ms,
        licenses=len(facts.licenses),
        dependencies=len(facts.dependencies),
        gaps=facts.gaps,
        verdicts=counts,
        cleared=cleared,
        notes=tuple(notes),
    )


def _task_output(crew_result: Any, index: int) -> str:
    """The raw text of one task's output, whichever shape CrewAI returned."""
    outputs = getattr(crew_result, "tasks_output", None) or []
    if index < len(outputs):
        return getattr(outputs[index], "raw", str(outputs[index])) or ""
    return ""
