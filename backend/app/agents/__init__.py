"""Agents, and the configurations the benchmark can ask for.

A stage is a callable ``(session, audit_id) -> object`` that adds findings to an
audit which has already been indexed and scanned. ``STAGES`` is the registry the
evaluation harness reads: a configuration is runnable exactly when its stage is
present here, so an unimplemented configuration is reported as unavailable
instead of scoring zero recall for a layer that never ran.

``single_agent_no_tools`` is the baseline (step 2.7, renamed in step 2.6f when it
stopped being the only single-agent configuration): one call over a context a
deterministic plan chose. ``single_agent_tools`` is the same single agent with the
crews' three tools and limits, covering security, architecture and license in one
task. ``multi_agent`` is the crew of agents (step 2.8's security reviewer, step
2.9's architecture reviewer, step 3.1's compliance reviewer), each with its own
model calls and outcome.
``scanners_plus_deterministic`` runs the module graph's cycle walk and no model at
all. ``multi_agent_validated`` joins this mapping when its verification pass lands.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy.orm import Session

from app.agents.architecture import ArchitectureOutcome, run_architecture_agent
from app.agents.baseline_single import AgentOutcome, run_baseline_single
from app.agents.compliance import ComplianceOutcome, run_compliance_agent
from app.agents.deterministic import DeterministicOutcome, run_deterministic_graph
from app.agents.multi import MultiAgentOutcome, run_multi_agent
from app.agents.security import SecurityOutcome, run_security_agent
from app.agents.single_tools import SingleToolsOutcome, run_single_agent_tools
from app.agents.validated import ValidatedOutcome, run_multi_agent_validated
from app.db.enums import EvalConfig

AgentStage = Callable[[Session, uuid.UUID], Any]

STAGES: Mapping[EvalConfig, AgentStage] = {
    EvalConfig.SINGLE_AGENT_NO_TOOLS: run_baseline_single,
    EvalConfig.SINGLE_AGENT_TOOLS: run_single_agent_tools,
    EvalConfig.MULTI_AGENT: run_multi_agent,
    EvalConfig.MULTI_AGENT_VALIDATED: run_multi_agent_validated,
    EvalConfig.SCANNERS_PLUS_DETERMINISTIC: run_deterministic_graph,
}

# The no-tools stage, kept as a name because the baseline is the default.
AGENT_STAGE: AgentStage = run_baseline_single

__all__ = [
    "AGENT_STAGE",
    "STAGES",
    "AgentOutcome",
    "AgentStage",
    "ArchitectureOutcome",
    "ComplianceOutcome",
    "DeterministicOutcome",
    "MultiAgentOutcome",
    "SecurityOutcome",
    "SingleToolsOutcome",
    "ValidatedOutcome",
    "run_architecture_agent",
    "run_baseline_single",
    "run_compliance_agent",
    "run_deterministic_graph",
    "run_multi_agent",
    "run_multi_agent_validated",
    "run_security_agent",
    "run_single_agent_tools",
]
