"""Agents, and the configurations the benchmark can ask for.

A stage is a callable ``(session, audit_id) -> object`` that adds findings to an
audit which has already been indexed and scanned. ``STAGES`` is the registry the
evaluation harness reads: a configuration is runnable exactly when its stage is
present here, so an unimplemented configuration is reported as unavailable
instead of scoring zero recall for a layer that never ran.

Today only ``single_agent`` exists (step 2.7). ``multi_agent`` and
``multi_agent_validated`` join this mapping when their stages land.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy.orm import Session

from app.agents.baseline_single import AgentOutcome, run_baseline_single
from app.db.enums import EvalConfig

AgentStage = Callable[[Session, uuid.UUID], Any]

STAGES: Mapping[EvalConfig, AgentStage] = {
    EvalConfig.SINGLE_AGENT: run_baseline_single,
}

# The single-agent stage, kept as a name because the baseline is the default.
AGENT_STAGE: AgentStage = run_baseline_single

__all__ = ["AGENT_STAGE", "STAGES", "AgentOutcome", "AgentStage", "run_baseline_single"]
