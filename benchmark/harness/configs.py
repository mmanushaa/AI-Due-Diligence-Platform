"""The configurations the harness can evaluate.

Six configurations are defined, because the benchmark's job is to show what each
layer buys:

    scanners_only                 index the snapshot, run the four scanners, report
                                  what they found. No model, no cost.
    single_agent_no_tools         scanners plus one reviewer agent over the whole
                                  repository, with the retrieval plan fixed in code.
    single_agent_tools            scanners plus one reviewer agent that chooses
                                  from the crews' three tools itself, in a single
                                  task covering security, architecture and license.
    multi_agent                   scanners plus the security, architecture and
                                  compliance agents, each with its own context.
    multi_agent_validated         multi_agent plus a second pass that re-checks each
                                  agent finding against fresh evidence.
    scanners_plus_deterministic   scanners plus the import cycles the module graph
                                  proves. No model, no cost.

``single_agent`` became ``single_agent_no_tools`` when the tool-using single agent
landed: the old name described a configuration that was no longer the only
single-agent one. It is the same stage and the same numbers, so its stored runs
stay comparable.

A configuration whose agent stage does not exist yet is *unavailable*, not empty:
running it would report zero recall for a layer that never ran, which would read
as "the agents add nothing" instead of "the agents are not built". Asking for one
therefore fails with an explicit reason.

The agent stage is plugged in through ``app.agents.STAGES``: a mapping from
configuration to a callable ``(session, audit_id) -> outcome`` that adds findings to
an already-scanned audit. A configuration becomes runnable the moment its entry
exists there, so nothing in this module has to change when a stage lands.

Every configuration also declares its ``limits``: which tools it may call, how many
turns and guardrail retries a crew gets, the reply-token budget, and the per-audit
spend ceiling. The runner applies the budget and stores the block in
``eval_runs.metrics["limits"]``, so a stored run says what it was allowed to do
instead of asking a reader to infer it from the code at the time.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from benchmark.harness import bootstrap

bootstrap.add_backend_to_path()

from app.db.enums import EvalConfig  # noqa: E402  (after sys.path is set up)

AGENT_REGISTRY = "app.agents.STAGES"
AGENT_STAGE_MODULE = "app.agents"

#: The crews' three read-only tools, by the names their schemas declare.
RETRIEVAL_TOOLS = ("retrieve_context", "get_symbol", "read_finding")
#: What a model-driven configuration is allowed to spend on one repository.
AGENT_BUDGET_USD = Decimal("0.25")
#: ``LLMClient`` is asked for this many reply tokens by every agent stage.
MAX_REPLY_TOKENS = 4_096
#: CrewAI turns per task, and guardrail retries after a refused answer.
CREW_MAX_ITER = 8
CREW_GUARDRAIL_RETRIES = 2
#: The baseline asks once, then once more after a rejected reply.
BASELINE_REPLY_RETRIES = 1


class ConfigurationUnavailable(RuntimeError):
    """The configuration needs a pipeline stage this checkout does not have."""


@dataclass(frozen=True)
class Limits:
    """What a configuration is allowed to use, recorded with every run.

    ``guardrail_retries`` and ``reply_retries`` are different mechanisms and are
    kept apart: a guardrail retry is CrewAI asking the same task again after it
    refused an answer, while a reply retry is a stage re-asking with the validator's
    reasons in the conversation. A scanners-only run uses neither, so both stay
    ``None`` rather than pretending to be zero.
    """

    tools: tuple[str, ...] = ()
    max_iter: int | None = None
    guardrail_retries: int | None = None
    reply_retries: int | None = None
    max_tokens: int | None = None
    budget_usd: Decimal | None = None
    calls_model: bool = False

    def as_json(self) -> dict[str, object]:
        """JSONB-friendly form for ``eval_runs.metrics["limits"]``."""
        return {
            "tools": list(self.tools),
            "max_iter": self.max_iter,
            "guardrail_retries": self.guardrail_retries,
            "reply_retries": self.reply_retries,
            "max_tokens": self.max_tokens,
            "budget_usd": None if self.budget_usd is None else str(self.budget_usd),
            "calls_model": self.calls_model,
        }


NO_MODEL = Limits()
CREW_LIMITS = Limits(
    tools=RETRIEVAL_TOOLS,
    max_iter=CREW_MAX_ITER,
    guardrail_retries=CREW_GUARDRAIL_RETRIES,
    reply_retries=0,
    max_tokens=MAX_REPLY_TOKENS,
    budget_usd=AGENT_BUDGET_USD,
    calls_model=True,
)
# The baseline has no tools and no guardrail; its one retry is the stage re-asking
# after the validator rejected the reply.
BASELINE_LIMITS = Limits(
    max_iter=None,
    guardrail_retries=None,
    reply_retries=BASELINE_REPLY_RETRIES,
    max_tokens=MAX_REPLY_TOKENS,
    budget_usd=AGENT_BUDGET_USD,
    calls_model=True,
)


@dataclass(frozen=True)
class Configuration:
    """One benchmark configuration and the stages it runs."""

    config: EvalConfig
    summary: str
    stages: tuple[str, ...]
    uses_agent_stage: bool
    limits: Limits = NO_MODEL

    @property
    def name(self) -> str:
        """The value stored in ``eval_runs.config``."""
        return self.config.value


CONFIGURATIONS: tuple[Configuration, ...] = (
    Configuration(
        config=EvalConfig.SCANNERS_ONLY,
        summary="index and the four scanners, no model call",
        stages=("index", "scanners"),
        uses_agent_stage=False,
    ),
    Configuration(
        config=EvalConfig.SINGLE_AGENT_NO_TOOLS,
        summary="scanners plus one reviewer agent for the whole repository, no tools",
        stages=("index", "scanners", "agent"),
        uses_agent_stage=True,
        limits=BASELINE_LIMITS,
    ),
    Configuration(
        config=EvalConfig.SINGLE_AGENT_TOOLS,
        summary="scanners plus one tool-using agent covering security, architecture and license",
        stages=("index", "scanners", "agent"),
        uses_agent_stage=True,
        limits=CREW_LIMITS,
    ),
    Configuration(
        config=EvalConfig.MULTI_AGENT,
        summary="scanners plus the security, architecture and compliance agents",
        stages=("index", "scanners", "agents"),
        uses_agent_stage=True,
        limits=CREW_LIMITS,
    ),
    Configuration(
        config=EvalConfig.MULTI_AGENT_VALIDATED,
        summary="multi_agent plus a validation pass over every agent finding",
        stages=("index", "scanners", "agents", "validation"),
        uses_agent_stage=True,
        limits=CREW_LIMITS,
    ),
    Configuration(
        config=EvalConfig.SCANNERS_PLUS_DETERMINISTIC,
        summary="scanners plus the import cycles the module graph proves, no model call",
        stages=("index", "scanners", "deterministic"),
        uses_agent_stage=True,
    ),
)

BY_NAME = {configuration.name: configuration for configuration in CONFIGURATIONS}


def get(name: str) -> Configuration:
    """Look a configuration up by its stored name."""
    try:
        return BY_NAME[name]
    except KeyError as exc:
        raise ConfigurationUnavailable(
            f"unknown configuration {name!r}; choose one of {', '.join(sorted(BY_NAME))}"
        ) from exc


def agent_stages() -> dict[EvalConfig, object]:
    """The stage registry, or an empty mapping when the agents package is absent."""
    import importlib

    try:
        module = importlib.import_module(AGENT_STAGE_MODULE)
    except ImportError:
        return {}
    return dict(getattr(module, "STAGES", {}) or {})


def availability(configuration: Configuration) -> tuple[bool, str | None]:
    """Whether this configuration can run here, and why not when it cannot."""
    if not configuration.uses_agent_stage:
        return True, None
    if configuration.config in agent_stages():
        return True, None
    return False, (
        f"{configuration.name} needs an agent stage registered in {AGENT_REGISTRY}, "
        "which this checkout does not have yet"
    )


def runnable() -> tuple[Configuration, ...]:
    """Every configuration that can run in this checkout today."""
    return tuple(item for item in CONFIGURATIONS if availability(item)[0])
