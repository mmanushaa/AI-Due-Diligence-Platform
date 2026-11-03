"""Step 2.5: gateway client — usage logging, the budget stop and the fallback.

Every test here replaces the gateway with a scripted provider, so the suite needs
no key and no network. What is under test is the client's own behaviour: what it
writes to ``agent_runs``, when it refuses to spend, and which model it reaches
for after a failure.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.core.config import Settings
from app.db.enums import AgentName, AuditStatus, SourceType
from app.db.models import AgentRun, Audit, Repository
from app.db.session import get_session_factory
from app.llm.client import (
    BudgetExceeded,
    Completion,
    LLMClient,
    LLMError,
    ProviderError,
    cost_usd,
    get_provider,
    price_for,
)

FIXTURES = Path(__file__).resolve().parents[1] / "benchmark" / "repos"

MESSAGES = [
    {"role": "system", "content": "You review code for authorization gaps."},
    {"role": "user", "content": "Is read_order protected?"},
]


@dataclass
class FakeProvider:
    """A gateway stand-in with scripted responses and failures."""

    completion: Completion = field(
        default_factory=lambda: Completion(text="looks fine", input_tokens=1_200, output_tokens=300)
    )
    # Model alias -> how many calls should fail before it starts answering.
    failures: dict[str, int] = field(default_factory=dict)
    error: ProviderError | None = None
    calls: list[dict] = field(default_factory=list)

    def complete(self, *, model, messages, max_tokens, temperature):
        self.calls.append(
            {"model": model, "messages": list(messages), "max_tokens": max_tokens}
        )
        remaining = self.failures.get(model, 0)
        if remaining:
            self.failures[model] = remaining - 1
            raise self.error or ProviderError("gateway call failed: HTTP 503 upstream unavailable")
        return self.completion


def settings_for(**overrides) -> Settings:
    """Explicit settings so a local .env cannot change what a test asserts."""
    base = {
        "llm_default_model": "gpt-4o-mini",
        "llm_triage_model": "gpt-4o-mini",
        "llm_fallback_model": "gpt-4o",
        "llm_fallback_enabled": True,
        "llm_max_attempts": 2,
        "llm_retry_base_seconds": 0.5,
        "llm_retry_max_seconds": 8.0,
        "llm_max_output_tokens": 1_024,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def make_audit():
    """Creates audits (with an optional budget) and removes them afterwards."""
    session = get_session_factory()()
    created: list[tuple[uuid.UUID, uuid.UUID]] = []

    def _make(budget: Decimal | None = None) -> uuid.UUID:
        repository = Repository(
            name=f"pytest-llm-{uuid.uuid4().hex[:8]}",
            source_type=SourceType.ZIP,
            storage_path=str(FIXTURES / "python_app"),
        )
        session.add(repository)
        session.commit()
        session.refresh(repository)

        audit = Audit(
            repository_id=repository.id, status=AuditStatus.QUEUED, budget_usd=budget
        )
        session.add(audit)
        session.commit()
        session.refresh(audit)
        created.append((audit.id, repository.id))
        return audit.id

    yield _make

    for audit_id, repository_id in created:
        session.delete(session.get(Audit, audit_id))
        session.delete(session.get(Repository, repository_id))
        session.commit()
    session.close()


def runs(session, audit_id: uuid.UUID) -> list[AgentRun]:
    return list(
        session.execute(
            select(AgentRun).where(AgentRun.audit_id == audit_id).order_by(AgentRun.created_at)
        ).scalars()
    )


# --- usage logging ---------------------------------------------------------


def test_one_call_logs_tokens_cost_and_latency(make_audit):
    audit_id = make_audit()
    provider = FakeProvider()
    sleeps: list[float] = []

    with get_session_factory()() as session:
        client = LLMClient(session, audit_id, provider=provider, settings=settings_for(), sleep=sleeps.append)
        result = client.complete(AgentName.SECURITY, MESSAGES)

        logged = runs(session, audit_id)
        assert len(logged) == 1
        run = logged[0]
        assert run.agent is AgentName.SECURITY
        assert run.model == "gpt-4o-mini"
        assert (run.input_tokens, run.output_tokens) == (1_200, 300)
        assert run.status == "succeeded"
        assert run.latency_ms is not None and run.latency_ms >= 0
        assert run.cost_usd == Decimal("0.0004")  # (1200*0.15 + 300*0.60) / 1e6

        assert result.text == "looks fine"
        assert result.model == "gpt-4o-mini"
        assert result.cost_usd == run.cost_usd
        assert result.latency_ms == run.latency_ms
        assert result.attempts == 1
        assert result.used_fallback is False

        # The audit total is derived from the logged rows, so the two agree.
        audit = session.get(Audit, audit_id)
        assert audit.cost_usd == run.cost_usd
        assert client.spent_usd() == run.cost_usd

    assert provider.calls[0]["model"] == "gpt-4o-mini"
    assert provider.calls[0]["messages"] == MESSAGES
    assert provider.calls[0]["max_tokens"] == 1_024
    assert sleeps == []


def test_the_gateway_cost_figure_wins_over_the_price_table(make_audit):
    audit_id = make_audit()
    provider = FakeProvider(
        completion=Completion(
            text="ok", input_tokens=1_200, output_tokens=300, reported_cost_usd=Decimal("0.0042")
        )
    )

    with get_session_factory()() as session:
        client = LLMClient(session, audit_id, provider=provider, settings=settings_for())
        result = client.complete(AgentName.SECURITY, MESSAGES)

        assert result.cost_usd == Decimal("0.0042")
        assert runs(session, audit_id)[0].cost_usd == Decimal("0.0042")


def test_triage_and_default_calls_use_their_own_alias(make_audit):
    audit_id = make_audit()
    provider = FakeProvider()

    with get_session_factory()() as session:
        client = LLMClient(
            session,
            audit_id,
            provider=provider,
            settings=settings_for(llm_triage_model="gpt-4o"),
        )
        client.complete(AgentName.SECURITY, MESSAGES, task="triage")
        client.complete(AgentName.SECURITY, MESSAGES)

    assert [call["model"] for call in provider.calls] == ["gpt-4o", "gpt-4o-mini"]


# --- budget ----------------------------------------------------------------


def test_a_tiny_budget_stops_the_run_before_the_next_call(make_audit):
    """One call is affordable by the check (0 < 0.0001), the next one is not."""
    audit_id = make_audit(budget=Decimal("0.0001"))
    provider = FakeProvider()

    with get_session_factory()() as session:
        client = LLMClient(session, audit_id, provider=provider, settings=settings_for())
        client.complete(AgentName.SECURITY, MESSAGES)

        with pytest.raises(BudgetExceeded) as excinfo:
            client.complete(AgentName.SECURITY, MESSAGES)

        error = excinfo.value
        assert error.audit_id == audit_id
        assert error.budget_usd == Decimal("0.0001")
        assert error.spent_usd == Decimal("0.0004")
        assert "0.0004" in str(error) and "0.0001" in str(error)

        # The stop happened before the network: one call, one logged row.
        assert len(provider.calls) == 1
        assert len(runs(session, audit_id)) == 1
        # Partial work is kept, which is what lets the run be labelled incomplete.
        assert client.spent_usd() == Decimal("0.0004")


def test_a_zero_budget_refuses_the_very_first_call(make_audit):
    audit_id = make_audit(budget=Decimal("0"))
    provider = FakeProvider()

    with get_session_factory()() as session:
        client = LLMClient(session, audit_id, provider=provider, settings=settings_for())
        with pytest.raises(BudgetExceeded):
            client.complete(AgentName.SECURITY, MESSAGES)
        assert runs(session, audit_id) == []

    assert provider.calls == []


def test_an_audit_without_a_budget_is_unlimited(make_audit):
    audit_id = make_audit()
    provider = FakeProvider()

    with get_session_factory()() as session:
        client = LLMClient(session, audit_id, provider=provider, settings=settings_for())
        assert client.budget_usd() is None
        assert client.remaining_usd() is None
        for _ in range(3):
            client.complete(AgentName.SECURITY, MESSAGES)

        assert client.spent_usd() == Decimal("0.0012")


def test_remaining_budget_shrinks_with_each_call(make_audit):
    audit_id = make_audit(budget=Decimal("1.0000"))
    provider = FakeProvider()

    with get_session_factory()() as session:
        client = LLMClient(session, audit_id, provider=provider, settings=settings_for())
        assert client.remaining_usd() == Decimal("1.0000")
        client.complete(AgentName.SECURITY, MESSAGES)
        assert client.remaining_usd() == Decimal("1.0000") - Decimal("0.0004")


def test_spending_is_per_audit(make_audit):
    first, second = make_audit(budget=Decimal("0.0001")), make_audit(budget=Decimal("1.0000"))
    provider = FakeProvider()

    with get_session_factory()() as session:
        spent_client = LLMClient(session, first, provider=provider, settings=settings_for())
        other_client = LLMClient(session, second, provider=provider, settings=settings_for())
        spent_client.complete(AgentName.SECURITY, MESSAGES)

        assert spent_client.spent_usd() == Decimal("0.0004")
        assert other_client.spent_usd() == Decimal("0")

        # The exhausted audit stops; the other one is unaffected.
        with pytest.raises(BudgetExceeded):
            spent_client.complete(AgentName.SECURITY, MESSAGES)
        other_client.complete(AgentName.SECURITY, MESSAGES)


# --- failures, retries, fallback -------------------------------------------


def test_a_forced_failure_falls_back_to_the_fallback_model(make_audit):
    audit_id = make_audit()
    provider = FakeProvider(failures={"gpt-4o-mini": 99})
    sleeps: list[float] = []

    with get_session_factory()() as session:
        client = LLMClient(
            session, audit_id, provider=provider, settings=settings_for(), sleep=sleeps.append
        )
        result = client.complete(AgentName.SECURITY, MESSAGES)

        assert result.used_fallback is True
        assert result.model == "gpt-4o"
        assert result.attempts == 3  # two failed attempts on the primary, one on the fallback
        assert [call["model"] for call in provider.calls] == ["gpt-4o-mini", "gpt-4o-mini", "gpt-4o"]

        logged = runs(session, audit_id)
        # One row per attempt; the order is asserted on the calls above, since
        # created_at is the transaction clock and rows can share a timestamp.
        assert Counter((run.model, run.status) for run in logged) == {
            ("gpt-4o-mini", "failed"): 2,
            ("gpt-4o", "succeeded"): 1,
        }
        # Failed attempts cost nothing; only the answered call is charged.
        failed = [run for run in logged if run.status == "failed"]
        assert [run.cost_usd for run in failed] == [None, None]
        succeeded = next(run for run in logged if run.status == "succeeded")
        assert succeeded.cost_usd == Decimal("0.0060")  # (1200*2.5 + 300*10) / 1e6

        # One backoff between the two attempts on the primary model, none after.
        assert sleeps == [0.5]


def test_retries_back_off_exponentially_and_then_give_up(make_audit):
    audit_id = make_audit()
    provider = FakeProvider(failures={"gpt-4o-mini": 99, "gpt-4o": 99})
    sleeps: list[float] = []

    with get_session_factory()() as session:
        client = LLMClient(
            session,
            audit_id,
            provider=provider,
            settings=settings_for(llm_max_attempts=3),
            sleep=sleeps.append,
        )
        with pytest.raises(LLMError) as excinfo:
            client.complete(AgentName.SECURITY, MESSAGES)

        message = str(excinfo.value)
        assert "gpt-4o-mini" in message and "gpt-4o" in message
        assert "after 6 attempt(s)" in message
        assert "HTTP 503" in message
        assert sleeps == [0.5, 1.0, 0.5, 1.0]

        logged = runs(session, audit_id)
        assert len(logged) == 6
        assert {run.status for run in logged} == {"failed"}
        assert session.get(Audit, audit_id).cost_usd == Decimal("0.0000")


def test_a_non_retryable_error_skips_retries_and_goes_to_the_fallback(make_audit):
    audit_id = make_audit()
    provider = FakeProvider(
        failures={"gpt-4o-mini": 99},
        error=ProviderError("gateway call failed: HTTP 400 bad request", retryable=False),
    )
    sleeps: list[float] = []

    with get_session_factory()() as session:
        client = LLMClient(
            session, audit_id, provider=provider, settings=settings_for(), sleep=sleeps.append
        )
        result = client.complete(AgentName.SECURITY, MESSAGES)

        assert result.model == "gpt-4o"
        assert [call["model"] for call in provider.calls] == ["gpt-4o-mini", "gpt-4o"]
        assert sleeps == []


def test_backoff_is_capped(make_audit):
    audit_id = make_audit()
    provider = FakeProvider(failures={"gpt-4o-mini": 99, "gpt-4o": 99})
    sleeps: list[float] = []

    with get_session_factory()() as session:
        client = LLMClient(
            session,
            audit_id,
            provider=provider,
            settings=settings_for(llm_max_attempts=6, llm_retry_base_seconds=1.0, llm_retry_max_seconds=2.0),
            sleep=sleeps.append,
        )
        with pytest.raises(LLMError):
            client.complete(AgentName.SECURITY, MESSAGES)

    assert sleeps == [1.0, 2.0, 2.0, 2.0, 2.0, 1.0, 2.0, 2.0, 2.0, 2.0]


def test_fallback_can_be_disabled(make_audit):
    audit_id = make_audit()
    provider = FakeProvider(failures={"gpt-4o-mini": 99})

    with get_session_factory()() as session:
        client = LLMClient(
            session,
            audit_id,
            provider=provider,
            settings=settings_for(llm_fallback_enabled=False),
            sleep=lambda _: None,
        )
        with pytest.raises(LLMError):
            client.complete(AgentName.SECURITY, MESSAGES)

    assert {call["model"] for call in provider.calls} == {"gpt-4o-mini"}


# --- inputs ----------------------------------------------------------------


def test_an_unconfigured_gateway_is_refused_without_echoing_the_key():
    with pytest.raises(LLMError):
        get_provider(Settings(litellm_base_url="", litellm_api_key=SecretStr("sk-secret-value")))
    with pytest.raises(LLMError):
        get_provider(Settings(litellm_base_url="http://127.0.0.1:9", litellm_api_key=None))


def test_a_transport_failure_is_retryable_and_never_echoes_the_key():
    provider = get_provider(
        Settings(litellm_base_url="http://127.0.0.1:9", litellm_api_key=SecretStr("sk-secret-value"))
    )
    with pytest.raises(ProviderError) as excinfo:
        provider.complete(model="gpt-4o-mini", messages=MESSAGES, max_tokens=8, temperature=0.0)

    assert excinfo.value.retryable is True
    assert "sk-secret-value" not in str(excinfo.value)


def test_an_unknown_agent_is_rejected_before_any_call(make_audit):
    audit_id = make_audit()
    provider = FakeProvider()

    with get_session_factory()() as session:
        client = LLMClient(session, audit_id, provider=provider, settings=settings_for())
        with pytest.raises(ValueError) as excinfo:
            client.complete("intruder", MESSAGES)

        assert "intruder" in str(excinfo.value)
        assert "security" in str(excinfo.value)

    assert provider.calls == []


def test_an_unknown_task_is_rejected(make_audit):
    audit_id = make_audit()

    with get_session_factory()() as session:
        client = LLMClient(session, audit_id, provider=FakeProvider(), settings=settings_for())
        with pytest.raises(ValueError):
            client.complete(AgentName.SECURITY, MESSAGES, task="summarise")


def test_a_missing_audit_is_a_clear_error(make_audit):
    with get_session_factory()() as session:
        client = LLMClient(
            session, uuid.uuid4(), provider=FakeProvider(), settings=settings_for()
        )
        with pytest.raises(LLMError) as excinfo:
            client.complete(AgentName.SECURITY, MESSAGES)

    assert "does not exist" in str(excinfo.value)


# --- pricing ---------------------------------------------------------------


def test_price_table_matches_the_most_specific_alias():
    assert price_for("gpt-4o-mini") == (Decimal("0.15"), Decimal("0.60"))
    assert price_for("gpt-4o") == (Decimal("2.50"), Decimal("10.00"))
    assert price_for("openai/gpt-4o-mini-2024-07-18") == (Decimal("0.15"), Decimal("0.60"))


def test_unknown_models_use_the_configured_default_price():
    settings = settings_for(llm_price_input_usd_per_1m=1.0, llm_price_output_usd_per_1m=2.0)

    assert price_for("some-local-model", settings) == (Decimal("1.0"), Decimal("2.0"))
    assert cost_usd("some-local-model", 1_000, 500, settings=settings) == Decimal("0.0020")


def test_cost_is_rounded_to_what_the_column_can_store():
    # 10 tokens at 0.15/1M is 0.0000015, below numeric(10,4).
    assert cost_usd("gpt-4o-mini", 10, 0) == Decimal("0.0000")
    assert cost_usd("gpt-4o", 1_000, 0) == Decimal("0.0025")
