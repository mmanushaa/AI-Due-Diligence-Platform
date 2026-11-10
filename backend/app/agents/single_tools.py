"""The ``single_agent_tools`` configuration (step 2.6f).

One agent, one task, the same three read-only tools the crews have, and the same
limits: eight turns, two guardrail retries, the same reply budget and the same
per-audit ceiling. They are imported from the security crew rather than copied, so
"the same" is a fact about the code and not a comment that drifts.

What it does not get is the crews' division of labour. One task has to cover
security, architecture and license together, where the crews give each concern its
own prompt and, for security, its own triage task. That is exactly what makes the
configuration worth running: ``single_agent_no_tools`` fixes the retrieval plan in
code, so it measures one call against a context the model did not choose; this one
measures the tools with the model choosing what to read. Comparing the two
separates "the tools help" from "the crews' prompts help", and comparing this
against ``multi_agent`` shows what the extra tasks buy.

The guardrail is the crews' rule without the security crew's ``get_symbol``
requirement: a single answer that cites code must have searched the repository
first, but a license finding needs a manifest read, not an auth helper, so
demanding the symbol lookup would fail the very task this configuration is meant
to do.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.agents.baseline_single import AgentOutcome
from app.agents.crew import GatewayLLM, crew_llm, read_only_tools
from app.agents.events import record_final_output, record_guardrail_failure
from app.agents.prompts import load_prompt
from app.agents.schema import InvalidOutput, parse_report, validate_report
from app.agents.security import GUARDRAIL_RETRIES, MAX_ITERATIONS
from app.agents.store import ensure_index, store_findings
from app.agents.tools import RepositoryTools
from app.db.enums import AgentName
from app.llm.client import LLMClient, LLMError

logger = logging.getLogger(__name__)

SOURCE_TOOL = "single_agent_tools"
AGENT_ROLE = AgentName.SECURITY
TASK = "review"

_NOT_JSON = (
    "That is not the JSON object from the contract. Reply with "
    '{"findings": [...]} and nothing else - no prose, no Markdown list.'
)
_NOT_READ = (
    "You have not searched this repository. Call retrieve_context with a question "
    "about the code you are about to cite (for example \"route handler that returns "
    "a record by id\"), read what it returns, and only then answer."
)


def _refused(
    repository_tools: RepositoryTools, attempt: int, raw: Any, feedback: str
) -> tuple[bool, Any]:
    """Record the answer that was refused, then hand CrewAI the same refusal."""
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
    calls: list[str],
) -> Any:
    """One agent, one task, the crews' tools and limits."""
    from crewai import Agent, Crew, Process, Task

    prompt = load_prompt("security")
    attempts: dict[str, int] = {}

    reviewer = Agent(
        role=prompt["role"],
        goal=prompt["goal"],
        backstory=prompt["backstory"],
        tools=read_only_tools(repository_tools, calls, agent=AGENT_ROLE),
        llm=crew_llm(llm),
        allow_delegation=False,
        max_iter=MAX_ITERATIONS,
        verbose=False,
    )

    def read_before_answering(output):
        """The crews' guardrail: the contract's JSON, and code actually read."""
        raw = getattr(output, "raw", output) or ""
        attempts[TASK] = attempts.get(TASK, 0) + 1
        attempt = attempts[TASK]
        try:
            parse_report(str(raw))
        except InvalidOutput:
            return _refused(repository_tools, attempt, raw, _NOT_JSON)
        if "retrieve_context" not in calls:
            return _refused(repository_tools, attempt, raw, _NOT_READ)
        return True, output

    review = Task(
        description=prompt["review"],
        expected_output=prompt["json_contract"],
        agent=reviewer,
        guardrail=read_before_answering,
        guardrail_max_retries=GUARDRAIL_RETRIES,
    )
    return Crew(agents=[reviewer], tasks=[review], process=Process.sequential, verbose=False)


@dataclass(frozen=True)
class SingleToolsOutcome(AgentOutcome):
    """The baseline's outcome plus the notes this stage adds."""

    tool_calls: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        """JSON-friendly form for ``eval_runs.metrics``."""
        return {
            **super().as_dict(),
            "tool_calls": self.tool_calls,
            "notes": list(self.notes),
        }


def _task_output(crew_result: Any, index: int) -> str:
    """The raw text of one task's output, whichever shape CrewAI returned."""
    outputs = getattr(crew_result, "tasks_output", None) or []
    if index < len(outputs):
        return getattr(outputs[index], "raw", str(outputs[index])) or ""
    return ""


def run_single_agent_tools(
    session: Session,
    audit_id: uuid.UUID,
    *,
    client: LLMClient | None = None,
    tools: RepositoryTools | None = None,
    crew_factory: Any = None,
) -> SingleToolsOutcome:
    """Review one audit with one tool-using agent, in a single task."""
    repository_tools = tools or RepositoryTools(session, audit_id)
    if not ensure_index(session, audit_id, repository_tools):
        return SingleToolsOutcome(errors=("no readable snapshot to index",))

    model_client = client or LLMClient(session, audit_id)
    llm = GatewayLLM(model_client, agent=AGENT_ROLE)
    calls: list[str] = []
    started = time.monotonic()

    try:
        crew = (crew_factory or build_crew)(repository_tools, llm, calls)
        result = crew.kickoff()
    except LLMError as exc:
        logger.warning("single-agent tools stage could not call the model for %s: %s", audit_id, exc)
        return SingleToolsOutcome(
            calls=llm.calls, tool_calls=len(calls), errors=(str(exc),), notes=("crew aborted",)
        )
    except Exception as exc:  # noqa: BLE001 - a crew failure must not fail the audit
        logger.warning("single-agent tools stage failed for %s: %s", audit_id, exc)
        return SingleToolsOutcome(
            calls=llm.calls,
            tool_calls=len(calls),
            errors=(f"{type(exc).__name__}: {exc}"[:300],),
            notes=("crew aborted",),
        )

    latency_ms = int((time.monotonic() - started) * 1000)
    answer = _task_output(result, 0)
    record_final_output(session, audit_id, agent=AGENT_ROLE, task=TASK, raw=answer)
    logger.debug("single-agent tools answer: %s", answer[:4_000])

    try:
        report = parse_report(answer)
    except InvalidOutput as exc:
        logger.info("single-agent tools answer unparseable for %s: %s", audit_id, exc)
        return SingleToolsOutcome(
            model=llm.model,
            calls=llm.calls,
            tool_calls=len(calls),
            latency_ms=latency_ms,
            rejected=1,
            dropped=1,
            notes=(f"answer unparseable: {exc}"[:200],),
        )

    validation = validate_report(report, repository_tools)
    stored = store_findings(
        session, audit_id, validation.accepted, repository_tools, source_tool=SOURCE_TOOL
    )

    notes = []
    if validation.rejected:
        notes.append(f"{len(validation.rejected)} finding(s) rejected by the validator")
    if stored:
        notes.append(f"added {stored} new finding(s)")
    logger.info(
        "single-agent tools stage for audit %s: %d stored, %d rejected, %d calls, %d tool call(s)",
        audit_id,
        stored,
        len(validation.rejected),
        llm.calls,
        len(calls),
    )
    return SingleToolsOutcome(
        model=llm.model,
        calls=llm.calls,
        accepted=stored,
        rejected=len(validation.rejected),
        latency_ms=latency_ms,
        tool_calls=len(calls),
        notes=tuple(notes),
    )
