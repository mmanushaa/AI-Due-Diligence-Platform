"""The ``multi_agent_validated`` configuration (step 2.10).

The same crews as ``multi_agent``, then the validation pass over what they wrote.
It is a separate stage rather than a flag on ``multi_agent`` so the two stored
configurations stay comparable: one run says what the agents claimed, the other
says what survived a check against the code, and the difference between them is
the number the validation step exists to show.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.agents.multi import MultiAgentOutcome, run_multi_agent
from app.validation.validator import ValidationOutcome, validate_audit

logger = logging.getLogger(__name__)

STAGE = "multi_agent_validated"


@dataclass(frozen=True)
class ValidatedOutcome:
    """Both crews' outcome plus the validator's, reported as one stage."""

    agents: MultiAgentOutcome = field(default_factory=MultiAgentOutcome)
    validation: ValidationOutcome = field(default_factory=ValidationOutcome)

    @property
    def calls(self) -> int:
        return self.agents.calls + self.validation.calls

    @property
    def accepted(self) -> int:
        return self.agents.accepted

    @property
    def cost_usd(self) -> Decimal:
        return self.agents.cost_usd + self.validation.cost_usd

    @property
    def errors(self) -> tuple[str, ...]:
        return self.agents.errors

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly form for ``eval_runs.metrics``."""
        return {
            "stage": STAGE,
            "calls": self.calls,
            "accepted": self.accepted,
            "cost_usd": str(self.cost_usd),
            "errors": list(self.errors),
            "agents": self.agents.as_dict(),
            "validation": self.validation.as_dict(),
        }


def run_multi_agent_validated(session: Session, audit_id: uuid.UUID) -> ValidatedOutcome:
    """Run every crew, then check what they wrote against the cited code."""
    agents = run_multi_agent(session, audit_id)
    validation = validate_audit(session, audit_id, semantic=True)
    logger.info(
        "validated stage for audit %s: %d agent findings, %d verified, %d rejected",
        audit_id,
        validation.checked,
        validation.verified,
        validation.rejected,
    )
    return ValidatedOutcome(agents=agents, validation=validation)
