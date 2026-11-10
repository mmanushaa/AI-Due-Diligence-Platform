"""The ``scanners_plus_deterministic`` configuration (step 2.6f).

Scanners report what one file shows. The graph reports what no single file can:
the import cycles ``find_cycles`` proved, with the import statement that closes
each one read back out of the file. This configuration runs exactly those two
layers and nothing else - no model, no prompt, no judgement - so what it scores is
a floor the model-driven configurations have to beat.

The floor is worth having on its own. A run below it means the deterministic code
regressed, not that the model had a bad day, and a run above it says how much of
the result a buyer is paying a model for. It also keeps the cheapest useful
configuration honest: the cycles ``multi_agent`` reports partly come from this same
``cycle_findings``, so the two numbers are directly comparable.

Findings are stored through the agents' normal path, with ``source_tool`` naming
this producer, so nothing downstream has to know that no model was involved.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.agents.architecture import cycle_findings, read_facts
from app.agents.baseline_single import AgentOutcome
from app.agents.store import ensure_index, store_findings
from app.agents.tools import RepositoryTools

logger = logging.getLogger(__name__)

SOURCE_TOOL = "deterministic_graph"
TASK = "cycle_walk"


@dataclass(frozen=True)
class DeterministicOutcome(AgentOutcome):
    """The shape the graph had, and what it proved, with no model involved."""

    modules: int = 0
    imports: int = 0
    cycles: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        """JSON-friendly form for ``eval_runs.metrics``."""
        return {
            **super().as_dict(),
            "modules": self.modules,
            "imports": self.imports,
            "cycles": self.cycles,
            "notes": list(self.notes),
        }


def run_deterministic_graph(
    session: Session, audit_id: uuid.UUID, *, tools: RepositoryTools | None = None
) -> DeterministicOutcome:
    """Store the cycles the module graph proves, without calling a model."""
    repository_tools = tools or RepositoryTools(session, audit_id)
    if not ensure_index(session, audit_id, repository_tools):
        return DeterministicOutcome(errors=("no readable snapshot to index",))

    started = time.monotonic()
    facts = read_facts(session, audit_id, repository_tools)
    proved = cycle_findings(facts)
    stored = store_findings(
        session, audit_id, proved, repository_tools, source_tool=SOURCE_TOOL
    )
    latency_ms = int((time.monotonic() - started) * 1000)

    notes = (
        f"{len(facts.cycles)} cycle(s) proved by the module graph",
        f"added {stored} new finding(s)",
    )
    logger.info(
        "deterministic graph stage for audit %s: %d cycles, %d stored",
        audit_id,
        len(facts.cycles),
        stored,
    )
    return DeterministicOutcome(
        stage=SOURCE_TOOL,
        calls=0,
        accepted=stored,
        latency_ms=latency_ms,
        modules=facts.modules,
        imports=facts.imports,
        cycles=len(facts.cycles),
        notes=notes,
    )
