"""The configurations the harness can evaluate.

Four configurations are defined, because the benchmark's job is to show what each
layer buys:

    scanners_only         index the snapshot, run the four scanners, report what
                          they found. No model, no cost.
    single_agent          scanners plus one reviewer agent over the whole repo.
    multi_agent           scanners plus the security, architecture and compliance
                          agents, each with its own context.
    multi_agent_validated multi_agent plus a second pass that re-checks each
                          agent finding against fresh evidence.

A configuration whose agent stage does not exist yet is *unavailable*, not empty:
running it would report zero recall for a layer that never ran, which would read
as "the agents add nothing" instead of "the agents are not built". Asking for one
therefore fails with an explicit reason.

The agent stage is plugged in through ``app.agents.AGENT_STAGE``: a callable
``(session, audit_id) -> None`` that adds findings to an already-scanned audit.
When that lands, the three agent configurations start working with no change here.
"""

from __future__ import annotations

from dataclasses import dataclass

from benchmark.harness import bootstrap

bootstrap.add_backend_to_path()

from app.db.enums import EvalConfig  # noqa: E402  (after sys.path is set up)

AGENT_STAGE_NAME = "AGENT_STAGE"
AGENT_STAGE_MODULE = "app.agents"


class ConfigurationUnavailable(RuntimeError):
    """The configuration needs a pipeline stage this checkout does not have."""


@dataclass(frozen=True)
class Configuration:
    """One benchmark configuration and the stages it runs."""

    config: EvalConfig
    summary: str
    stages: tuple[str, ...]
    uses_agent_stage: bool

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
        config=EvalConfig.SINGLE_AGENT,
        summary="scanners plus one reviewer agent for the whole repository",
        stages=("index", "scanners", "agent"),
        uses_agent_stage=True,
    ),
    Configuration(
        config=EvalConfig.MULTI_AGENT,
        summary="scanners plus the security, architecture and compliance agents",
        stages=("index", "scanners", "agents"),
        uses_agent_stage=True,
    ),
    Configuration(
        config=EvalConfig.MULTI_AGENT_VALIDATED,
        summary="multi_agent plus a validation pass over every agent finding",
        stages=("index", "scanners", "agents", "validation"),
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


def agent_stage():
    """Return ``app.agents.AGENT_STAGE`` when the agent pipeline exists."""
    import importlib

    try:
        module = importlib.import_module(AGENT_STAGE_MODULE)
    except ImportError:
        return None
    return getattr(module, AGENT_STAGE_NAME, None)


def availability(configuration: Configuration) -> tuple[bool, str | None]:
    """Whether this configuration can run here, and why not when it cannot."""
    if not configuration.uses_agent_stage:
        return True, None
    if agent_stage() is None:
        return False, (
            f"{configuration.name} needs the agent pipeline "
            f"({AGENT_STAGE_MODULE}.{AGENT_STAGE_NAME}), which this checkout does not have yet"
        )
    return True, None


def runnable() -> tuple[Configuration, ...]:
    """Every configuration that can run in this checkout today."""
    return tuple(item for item in CONFIGURATIONS if availability(item)[0])
