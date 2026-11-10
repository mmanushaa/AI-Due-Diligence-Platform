"""Step 2.6f: the limits every run records, checked against the code that runs.

``eval_runs.metrics["limits"]`` is only worth reading if it describes what the
stage actually did. These tests compare the declared block with the constants the
stages import, so a number changed in one place and not the other is a failure
instead of a table that quietly lies about its own fairness.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.agents import STAGES, architecture, baseline_single, security
from app.agents.crew import read_only_tools
from app.agents.deterministic import run_deterministic_graph
from app.agents.single_tools import run_single_agent_tools
from app.db.enums import EvalConfig
from benchmark.harness import configs


class _NoDatabase:
    """Just enough of RepositoryTools to build the tool schemas."""

    session = None
    audit_id = None


def test_the_tool_names_are_the_ones_the_stage_declares():
    names = tuple(tool.name for tool in read_only_tools(_NoDatabase()))
    assert names == configs.RETRIEVAL_TOOLS


def test_the_single_agent_tools_configuration_has_the_crews_tools_and_limits():
    limits = configs.get("single_agent_tools").limits

    assert limits.tools == configs.RETRIEVAL_TOOLS
    assert limits.max_iter == security.MAX_ITERATIONS == architecture.MAX_ITERATIONS
    assert (
        limits.guardrail_retries
        == security.GUARDRAIL_RETRIES
        == architecture.GUARDRAIL_RETRIES
    )
    assert limits.max_tokens == baseline_single.MAX_REPLY_TOKENS
    assert limits.budget_usd == Decimal("0.25")
    assert limits.calls_model is True


def test_the_no_tools_baseline_keeps_its_own_retry_and_no_tools():
    limits = configs.get("single_agent_no_tools").limits

    assert limits.tools == ()
    assert limits.max_iter is None
    assert limits.guardrail_retries is None
    assert limits.reply_retries == baseline_single.RETRIES
    assert limits.max_tokens == baseline_single.MAX_REPLY_TOKENS
    assert limits.budget_usd == Decimal("0.25")


def test_the_configurations_that_call_no_model_declare_no_tools_and_no_budget():
    for name in ("scanners_only", "scanners_plus_deterministic"):
        limits = configs.get(name).limits
        assert limits == configs.NO_MODEL, name
        assert limits.as_json() == {
            "tools": [],
            "max_iter": None,
            "guardrail_retries": None,
            "reply_retries": None,
            "max_tokens": None,
            "budget_usd": None,
            "calls_model": False,
        }, name


def test_both_new_configurations_are_registered_and_runnable():
    assert STAGES[EvalConfig.SINGLE_AGENT_TOOLS] is run_single_agent_tools
    assert STAGES[EvalConfig.SCANNERS_PLUS_DETERMINISTIC] is run_deterministic_graph
    for name in ("single_agent_tools", "scanners_plus_deterministic"):
        assert configs.availability(configs.get(name)) == (True, None), name


def test_the_old_single_agent_name_is_gone_rather_than_kept_as_a_synonym():
    with pytest.raises(configs.ConfigurationUnavailable, match="unknown configuration"):
        configs.get("single_agent")


def test_five_configurations_can_run_and_the_validation_pass_is_the_one_that_cannot():
    names = tuple(configuration.name for configuration in configs.runnable())
    assert names == (
        "scanners_only",
        "single_agent_no_tools",
        "single_agent_tools",
        "multi_agent",
        "scanners_plus_deterministic",
    )
    available, reason = configs.availability(configs.get("multi_agent_validated"))
    assert available is False and configs.AGENT_REGISTRY in (reason or "")
