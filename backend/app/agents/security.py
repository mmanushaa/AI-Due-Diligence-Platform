"""The security agent, as a CrewAI crew (step 2.8).

Two tasks, one reviewer:

    triage       go through every security and secret finding the scanners
                 produced and say whether each is likely real, likely a false
                 positive, or uncertain, citing the code
    investigate  look for what one file cannot show - a handler that reads or
                 changes a row without authenticating the caller or checking
                 ownership, an import cycle, a committed credential - and report
                 new findings

The tools are the three read-only ones from step 2.7 and nothing else: retrieval,
symbol lookup, and reading a stored finding. There is no shell tool, no file
reader, no browser, no write path, so the worst a confused or injected model can
do is ask for context it already had. Repository text reaches it only inside the
same delimited data blocks the baseline uses, because the tool returns rendered
blocks and the prompt says what they are.

Every model call still goes through :class:`~app.llm.client.LLMClient`: the crew's
LLM is a thin adapter that forwards to it, so the budget check, the ``agent_runs``
row per attempt and the fallback path all behave exactly as they do for the
baseline. CrewAI is the orchestration, not a second door to the provider.

Validation is the same gate the baseline uses: :func:`~app.agents.schema.parse_report`
then :func:`~app.agents.schema.validate_report`, so a finding that cites a file the
audit does not have, a line past the end of the file, or a quote that is not at the
cited lines is dropped rather than stored. A crew answer is retried once when
nothing in it survives, because a crew that produced only unverifiable findings has
usually been asked for the wrong shape rather than having nothing to say.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.agents.baseline_single import AgentOutcome
from app.agents.crew import GatewayLLM, crew_llm, read_only_tools
from app.agents.prompts import fill, load_prompt
from app.agents.schema import AgentFinding, InvalidOutput, parse_report, validate_report
from app.agents.store import ensure_index, store_findings
from app.agents.tools import FindingView, RepositoryTools
from app.db.enums import AgentName, Category
from app.llm.client import LLMClient, LLMError

logger = logging.getLogger(__name__)

SOURCE_TOOL = "security_agent"
AGENT_ROLE = AgentName.SECURITY
MAX_ITERATIONS = 8
TRIAGE_LIMIT = 24
#: ``CREW_VERBOSE=1`` prints each ReAct step, which is how a prompt that stops the
#: model from using its tools gets caught. Off by default: the steps are long.
VERBOSE = os.environ.get("CREW_VERBOSE", "").lower() in {"1", "true", "yes"}
TRIAGE_CATEGORIES = (Category.SECURITY, Category.SECRET)
_RETRY_NOTE = (
    "\n\nYour previous answer was rejected by the validator. Reply again with the "
    "complete JSON object, citing only files and line numbers that appeared in a "
    "tool result."
)


@dataclass(frozen=True)
class TriageVerdict:
    """One scanner finding, judged."""

    finding_id: str
    verdict: str
    reasoning: str


def _parse_verdicts(raw: str) -> tuple[TriageVerdict, ...]:
    """Read the triage task's JSON, tolerating prose around it."""
    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return ()
    try:
        body = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return ()
    verdicts: list[TriageVerdict] = []
    for item in body.get("verdicts") or []:
        if not isinstance(item, dict):
            continue
        verdict = str(item.get("verdict", "")).strip().lower()
        if verdict not in {"likely_real", "likely_false_positive", "uncertain"}:
            verdict = "uncertain"
        verdicts.append(
            TriageVerdict(
                finding_id=str(item.get("finding_id", "")),
                verdict=verdict,
                reasoning=" ".join(str(item.get("reasoning", "")).split())[:400],
            )
        )
    return tuple(verdicts)


def _finding_lines(views: tuple[FindingView, ...]) -> str:
    return "\n".join(
        f"- id={view.finding_id} [{view.category}/{view.severity}] {view.title} "
        f"at {view.file_path}:{view.line_start}-{view.line_end}"
        for view in views
    )


def build_crew(
    repository_tools: RepositoryTools,
    llm: GatewayLLM,
    findings: tuple[FindingView, ...],
) -> Any:
    """Assemble the one-agent, two-task crew for this audit."""
    from crewai import Agent, Crew, Process, Task

    prompt = load_prompt("security")
    calls: list[str] = []
    reviewer = Agent(
        role=prompt["role"],
        goal=prompt["goal"],
        backstory=prompt["backstory"],
        tools=read_only_tools(repository_tools, calls),
        llm=crew_llm(llm),
        allow_delegation=False,
        max_iter=MAX_ITERATIONS,
        verbose=VERBOSE,
    )

    def forget_triage_calls(output: Any) -> Any:
        """Start the second task's tool log empty, after the first task ends."""
        calls.clear()
        return output

    def read_before_answering(output):
        """A crew that answers without reading anything has not investigated.

        Without this the investigation task returns the repository's most obvious
        problem, cribbed from the turn it just finished, and the run looks like a
        successful review of code it never opened.
        """
        raw = getattr(output, "raw", output) or ""
        try:
            parse_report(str(raw))
        except InvalidOutput:
            return False, (
                "That is not the JSON object from the contract. Reply with "
                '{"findings": [...]} and nothing else - no prose, no Markdown list.'
            )
        if "retrieve_context" not in calls:
            return False, (
                "You have not searched this repository. Call retrieve_context with a "
                "question about its route handlers (for example \"route handler that "
                "returns a record by id\"), read the code it returns, and only then "
                "answer. Re-reading a finding you already triaged is not an "
                "investigation."
            )
        if "get_symbol" not in calls:
            return False, (
                "You have not compared the handler against the repository's auth "
                "helper. Call get_symbol with the helper's name (usually "
                "get_current_user), look at which modules reference it, and cite the "
                "handler that is missing it, not the helper that defines it."
            )
        return True, output

    triage = Task(
        description=fill(prompt["triage"], findings=_finding_lines(findings) or "none"),
        expected_output=prompt["triage_contract"],
        agent=reviewer,
        callback=forget_triage_calls,
    )
    investigate = Task(
        description=prompt["investigation"],
        expected_output=prompt["json_contract"],
        agent=reviewer,
        guardrail=read_before_answering,
        guardrail_max_retries=2,
    )
    return Crew(
        agents=[reviewer],
        tasks=[triage, investigate],
        process=Process.sequential,
        verbose=VERBOSE,
    )


@dataclass(frozen=True)
class SecurityOutcome(AgentOutcome):
    """The baseline's outcome plus what the security crew added."""

    triaged: int = 0
    likely_real: int = 0
    likely_false_positive: int = 0
    uncertain: int = 0
    verdicts: tuple[TriageVerdict, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        """JSON-friendly form for ``eval_runs.metrics``."""
        return {
            **super().as_dict(),
            "triaged": self.triaged,
            "likely_real": self.likely_real,
            "likely_false_positive": self.likely_false_positive,
            "uncertain": self.uncertain,
            "verdicts": [
                {"finding_id": item.finding_id, "verdict": item.verdict, "reasoning": item.reasoning}
                for item in self.verdicts
            ],
            "notes": list(self.notes),
        }


def _task_output(crew_result: Any, index: int) -> str:
    """The raw text of one task's output, whichever shape CrewAI returned."""
    outputs = getattr(crew_result, "tasks_output", None) or []
    if index < len(outputs):
        return getattr(outputs[index], "raw", str(outputs[index])) or ""
    return ""


def run_security_agent(
    session: Session,
    audit_id: uuid.UUID,
    *,
    client: LLMClient | None = None,
    tools: RepositoryTools | None = None,
    crew_factory: Any = None,
) -> SecurityOutcome:
    """Run the security crew over an indexed, scanned audit."""
    repository_tools = tools or RepositoryTools(session, audit_id)
    if not ensure_index(session, audit_id, repository_tools):
        return SecurityOutcome(errors=("no readable snapshot to index",))

    findings = tuple(
        view
        for view in repository_tools.findings(limit=40)
        if view.category in {category.value for category in TRIAGE_CATEGORIES}
    )[:TRIAGE_LIMIT]

    model_client = client or LLMClient(session, audit_id)
    llm = GatewayLLM(model_client, agent=AGENT_ROLE)
    started = time.monotonic()

    try:
        crew = (crew_factory or build_crew)(repository_tools, llm, findings)
        result = crew.kickoff()
    except LLMError as exc:
        logger.warning("security crew could not call the model for %s: %s", audit_id, exc)
        return SecurityOutcome(calls=llm.calls, errors=(str(exc),), notes=("crew aborted",))
    except Exception as exc:  # noqa: BLE001 - a crew failure must not fail the audit
        logger.warning("security crew failed for %s: %s", audit_id, exc)
        return SecurityOutcome(
            calls=llm.calls, errors=(f"{type(exc).__name__}: {exc}"[:300],), notes=("crew aborted",)
        )

    latency_ms = int((time.monotonic() - started) * 1000)
    triage_raw = _task_output(result, 0)
    investigation_raw = _task_output(result, 1)
    logger.debug("security triage answer: %s", triage_raw[:2_000])
    logger.debug("security investigation answer: %s", investigation_raw[:4_000])
    verdicts = _parse_verdicts(triage_raw)
    counts = {
        "likely_real": sum(1 for item in verdicts if item.verdict == "likely_real"),
        "likely_false_positive": sum(
            1 for item in verdicts if item.verdict == "likely_false_positive"
        ),
        "uncertain": sum(1 for item in verdicts if item.verdict == "uncertain"),
    }

    accepted: tuple[AgentFinding, ...] = ()
    rejected = 0
    try:
        report = parse_report(investigation_raw)
    except InvalidOutput as exc:
        logger.info("security crew answer unparseable for %s: %s", audit_id, exc)
        return SecurityOutcome(
            calls=llm.calls,
            latency_ms=latency_ms,
            triaged=len(verdicts),
            verdicts=verdicts,
            rejected=1,
            dropped=1,
            notes=(f"investigation answer unparseable: {exc}"[:200],),
            **counts,
        )

    validation = validate_report(report, repository_tools)
    accepted, rejected = validation.accepted, len(validation.rejected)
    stored = store_findings(
        session, audit_id, accepted, repository_tools, source_tool=SOURCE_TOOL
    )

    notes = []
    if rejected:
        notes.append(f"{rejected} finding(s) rejected by the validator")
    if stored:
        notes.append(f"added {stored} new finding(s)")
    logger.info(
        "security crew for audit %s: %d triaged, %d stored, %d rejected, %d calls",
        audit_id,
        len(verdicts),
        stored,
        rejected,
        llm.calls,
    )
    return SecurityOutcome(
        model=llm.model,
        calls=llm.calls,
        accepted=stored,
        rejected=rejected,
        latency_ms=latency_ms,
        triaged=len(verdicts),
        verdicts=verdicts,
        notes=tuple(notes),
        **counts,
    )


def looks_like_authorization(finding: AgentFinding) -> bool:
    """Whether a finding is the planted 'no authz check' shape.

    Used by the stage outcome rather than by scoring: the harness already decides
    matches, this only lets a run say out loud whether the crew went after the
    thing it was sent to find.
    """
    text = f"{finding.title} {finding.reasoning}".lower()
    return bool(re.search(r"auth|ownership|caller|permission|current_user|access", text))
