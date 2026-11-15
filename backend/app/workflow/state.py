"""The workflow's state (step 3.2).

LangGraph checkpoints this model after every node, so it has to hold everything a
resumed run needs and nothing that cannot be serialised: ids as strings, counters as
numbers, and the gaps the run accumulated rather than the exceptions that caused
them.

``completed`` is the audit trail a resume reads: a node appends its own name, so a
restarted run knows which stages already ran without re-deriving it from the
database.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AuditState(BaseModel):
    """What one audit run knows at a checkpoint."""

    audit_id: str
    repository_id: str | None = None
    snapshot: str | None = None

    status: str = "queued"
    stage: str | None = None
    completed: list[str] = Field(default_factory=list)

    #: Where the route node sent the run: "agents" or "skip".
    route: str = "agents"

    files_included: int = 0
    files_excluded: int = 0

    #: Scanner that failed, and why. A failure here does not stop the run.
    scanner_failures: dict[str, str] = Field(default_factory=dict)

    agent_calls: int = 0
    agent_accepted: int = 0
    agent_errors: list[str] = Field(default_factory=list)

    validation: dict[str, int] = Field(default_factory=dict)
    #: Findings a model failure left unproven; kept apart from the validation pass's
    #: own counts because they were never checked by it.
    agent_unresolved: int = 0
    risk: dict[str, int] = Field(default_factory=dict)
    risk_level: str | None = None
    assessment_id: str | None = None

    #: The human review gate (step 3.4): what the reviewer decided, if anything.
    review_decision: str | None = None
    review_id: str | None = None

    budget_exceeded: bool = False
    #: True when the run finished on partial data; the status says so too.
    incomplete: bool = False
    incomplete_reason: str | None = None
    errors: list[str] = Field(default_factory=list)

    review: str = "pending"  # step 3.4's gate: not_required, pending, approved, ...
    report: str = "pending"  # stub node (step 3.5)
