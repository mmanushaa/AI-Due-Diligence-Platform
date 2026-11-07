"""The ``multi_agent`` configuration: every crew, one stage (steps 2.8-2.9).

The harness runs one stage per configuration, so the crews are composed here rather
than by teaching the harness about a list of agents. Each crew keeps its own model
calls, its own outcome and its own source tool, which is what makes the per-agent
lines in a benchmark report mean something; this module only runs them in order and
adds their numbers up.

The order follows the cost: the security crew is the expensive one and its findings
are why the configuration exists. The architecture crew's facts cost no model calls
at all, so a budget that runs out on the security crew still leaves the graph's
cycles reportable after it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.agents.architecture import ArchitectureOutcome, run_architecture_agent
from app.agents.security import SecurityOutcome, run_security_agent

logger = logging.getLogger(__name__)

STAGE = "multi_agent"


@dataclass(frozen=True)
class MultiAgentOutcome:
    """Both crews, reported as one stage with a per-agent breakdown."""

    security: SecurityOutcome = field(default_factory=SecurityOutcome)
    architecture: ArchitectureOutcome = field(default_factory=ArchitectureOutcome)

    @property
    def calls(self) -> int:
        return self.security.calls + self.architecture.calls

    @property
    def accepted(self) -> int:
        return self.security.accepted + self.architecture.accepted

    @property
    def cost_usd(self) -> Decimal:
        return self.security.cost_usd + self.architecture.cost_usd

    @property
    def errors(self) -> tuple[str, ...]:
        """Every failure, labelled with the crew that had it."""
        return tuple(
            f"{name}: {error}"
            for name, outcome in (("security", self.security), ("architecture", self.architecture))
            for error in outcome.errors
        )

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly form for ``eval_runs.metrics``."""
        return {
            "stage": STAGE,
            "model": self.security.model or self.architecture.model,
            "calls": self.calls,
            "accepted": self.accepted,
            "rejected": self.security.rejected + self.architecture.rejected,
            "dropped": self.security.dropped + self.architecture.dropped,
            "cost_usd": str(self.cost_usd),
            "latency_ms": self.security.latency_ms + self.architecture.latency_ms,
            "errors": list(self.errors),
            "notes": [
                f"{name}: {note}"
                for name, outcome in (
                    ("security", self.security),
                    ("architecture", self.architecture),
                )
                for note in outcome.notes
            ],
            "agents": {
                "security": self.security.as_dict(),
                "architecture": self.architecture.as_dict(),
            },
        }


def run_multi_agent(session: Session, audit_id: uuid.UUID) -> MultiAgentOutcome:
    """Run every crew over one audit, in order, and report them together."""
    security = run_security_agent(session, audit_id)
    architecture = run_architecture_agent(session, audit_id)
    logger.info(
        "multi-agent stage for audit %s: security %d stored, architecture %d stored, %d calls",
        audit_id,
        security.accepted,
        architecture.accepted,
        security.calls + architecture.calls,
    )
    return MultiAgentOutcome(security=security, architecture=architecture)
