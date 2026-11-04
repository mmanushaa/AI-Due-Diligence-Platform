"""LLM access through the LiteLLM gateway (step 2.5).

Every model call an audit makes goes through one client, and the client owns the
three things a bare SDK call does not:

* **the budget.** ``audits.budget_usd`` is a hard stop, checked before each call
  against what the audit has already spent. When it is reached the client raises
  :class:`BudgetExceeded` *before* spending anything else, so the workflow can
  stop cleanly and keep the partial results it already has.
* **the failure path.** A provider error is retried with exponential backoff,
  then the call is repeated against the fallback model, and only then does it
  fail as :class:`LLMError`.
* **the record.** One ``agent_runs`` row per attempt, so tokens, cost and
  latency stay attributable to an agent and a model after the run.

Routing is by alias, not by provider model id: ``docker/litellm/config.yaml``
owns the alias-to-provider mapping, so switching provider is a gateway change.
No key appears in this module — the gateway is reached with the key held in
``Settings.litellm_api_key``, which is never logged or put in an error message.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, settings as default_settings
from app.db.enums import AgentName
from app.db.models import AgentRun, Audit

# `cost_usd` is numeric(10,4), so this is the smallest amount the column can
# hold; anything finer is rounded rather than silently truncated.
COST_QUANTUM = Decimal("0.0001")
PER_MILLION = Decimal(1_000_000)

TASK_DEFAULT = "default"
TASK_TRIAGE = "triage"

STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

# USD per 1M tokens as (input, output). LiteLLM reports the real cost for models
# it knows, and that number wins when present; this table is the offline
# estimate used when it is not. Longest alias first so "gpt-4o-mini" is not
# matched by "gpt-4o".
MODEL_PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
    "gpt-4.1-mini": (Decimal("0.40"), Decimal("1.60")),
    "gpt-4o": (Decimal("2.50"), Decimal("10.00")),
    "gpt-4.1": (Decimal("2.00"), Decimal("8.00")),
    "claude-3-5-haiku": (Decimal("0.80"), Decimal("4.00")),
    "claude-sonnet": (Decimal("3.00"), Decimal("15.00")),
    # Free tier on Together: PrismML's ternary Bonsai. Listed at zero so a run
    # against it reports what it actually costs instead of the fallback rate.
    "bonsai": (Decimal("0"), Decimal("0")),
}
_PRICE_ALIASES = sorted(MODEL_PRICES, key=len, reverse=True)


class LLMError(RuntimeError):
    """An LLM call could not be completed. Never contains a credential."""


class ProviderError(LLMError):
    """The gateway refused or broke the call.

    ``retryable`` separates "the provider is busy" from "this request will never
    succeed": a 400 on a malformed prompt should go straight to the fallback
    model instead of burning the retry budget.
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class BudgetExceeded(RuntimeError):
    """The audit has no budget left, so no further call is made.

    Deliberately not an ``LLMError``: a caller that retries or degrades on LLM
    failures must not swallow the budget stop.
    """

    def __init__(self, audit_id: uuid.UUID, budget_usd: Decimal, spent_usd: Decimal) -> None:
        super().__init__(
            f"audit {audit_id} has spent ${spent_usd} of its ${budget_usd} budget; "
            "stopping before the next call"
        )
        self.audit_id = audit_id
        self.budget_usd = budget_usd
        self.spent_usd = spent_usd


@dataclass(frozen=True)
class Completion:
    """One successful provider response, before the client prices it."""

    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    # The gateway's own cost figure (LiteLLM `_hidden_params.response_cost`).
    reported_cost_usd: Decimal | None = None


@dataclass(frozen=True)
class LLMResult:
    """What an agent gets back, including what the call cost."""

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    latency_ms: int
    attempts: int
    used_fallback: bool = False


class ChatProvider(Protocol):
    """The one method the client needs from a model backend."""

    def complete(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, str]],
        max_tokens: int,
        temperature: float,
    ) -> Completion:
        """Return one completion or raise :class:`ProviderError`."""


# --- pricing ---------------------------------------------------------------


def price_for(model: str, settings: Settings | None = None) -> tuple[Decimal, Decimal]:
    """USD per 1M tokens for a model alias, falling back to the configured price."""
    lowered = model.lower()
    for alias in _PRICE_ALIASES:
        if alias in lowered:
            return MODEL_PRICES[alias]
    settings = settings or default_settings
    return (
        Decimal(str(settings.llm_price_input_usd_per_1m)),
        Decimal(str(settings.llm_price_output_usd_per_1m)),
    )


def as_usd(value: Decimal | float | str) -> Decimal:
    """Round to what numeric(10,4) can actually store."""
    return Decimal(str(value)).quantize(COST_QUANTUM, rounding=ROUND_HALF_UP)


def cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    reported: Decimal | None = None,
    settings: Settings | None = None,
) -> Decimal:
    """Cost of one call, preferring the gateway's figure over the price table."""
    if reported is not None:
        return as_usd(reported)
    input_price, output_price = price_for(model, settings)
    value = (
        Decimal(int(input_tokens or 0)) * input_price
        + Decimal(int(output_tokens or 0)) * output_price
    ) / PER_MILLION
    return as_usd(value)


# --- gateway provider ------------------------------------------------------


class GatewayChatProvider:
    """Calls an OpenAI-compatible ``/chat/completions`` on the LiteLLM gateway."""

    def __init__(self, *, base_url: str, api_key: str, timeout_seconds: float = 60.0) -> None:
        if not base_url:
            raise LLMError("LITELLM_BASE_URL is required for the LLM gateway")
        if not api_key:
            raise LLMError("LITELLM_API_KEY is required for the LLM gateway")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key  # never logged, never returned, never in a message
        self.timeout_seconds = timeout_seconds

    def complete(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, str]],
        max_tokens: int,
        temperature: float,
    ) -> Completion:
        payload = json.dumps(
            {
                "model": model,
                "messages": [dict(message) for message in messages],
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        ).encode("utf8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            # Status and a short body are safe to keep; the headers are not echoed.
            detail = exc.read().decode("utf8", "replace")[:200]
            retryable = exc.code in (408, 409, 429) or exc.code >= 500
            raise ProviderError(
                f"gateway call failed: HTTP {exc.code} {detail}", retryable=retryable
            ) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"gateway call failed: {exc.reason}", retryable=True) from exc
        except (TimeoutError, OSError) as exc:
            # A read timeout arrives as a bare TimeoutError, not a URLError: the
            # connection was made and the gateway simply took longer than
            # LLM_TIMEOUT_SECONDS to answer. Slow providers do this, and it must
            # fall back like any other transient failure instead of killing the
            # audit with an unhandled socket error.
            raise ProviderError(
                f"gateway call timed out after {self.timeout_seconds}s", retryable=True
            ) from exc
        except http.client.HTTPException as exc:
            raise ProviderError(
                f"gateway call failed: {type(exc).__name__}", retryable=True
            ) from exc
        except json.JSONDecodeError as exc:
            raise ProviderError("gateway response was not JSON", retryable=False) from exc

        return self._parse(body)

    def _parse(self, body: dict) -> Completion:
        choices = body.get("choices") or []
        if not choices:
            raise ProviderError("gateway response had no choices", retryable=False)
        message = choices[0].get("message") or {}
        text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("gateway response had no text", retryable=False)
        usage = body.get("usage") or {}
        hidden = body.get("_hidden_params") or {}
        reported = hidden.get("response_cost")
        return Completion(
            text=text.strip(),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            reported_cost_usd=Decimal(str(reported)) if reported is not None else None,
        )


def get_provider(settings: Settings | None = None) -> ChatProvider:
    """The gateway provider built from settings; refuses to start without a key."""
    settings = settings or default_settings
    api_key = settings.litellm_api_key.get_secret_value() if settings.litellm_api_key else ""
    return GatewayChatProvider(
        base_url=settings.litellm_base_url or "",
        api_key=api_key,
        timeout_seconds=settings.llm_timeout_seconds,
    )


# --- the client ------------------------------------------------------------


class LLMClient:
    """Budgeted, logged, fallback-capable model calls for one audit."""

    def __init__(
        self,
        session: Session,
        audit_id: uuid.UUID,
        *,
        provider: ChatProvider | None = None,
        settings: Settings | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.session = session
        self.audit_id = audit_id
        self.settings = settings or default_settings
        self.provider = provider or get_provider(self.settings)
        # Injected so retry tests do not actually wait.
        self._sleep = sleep

    # -- budget ------------------------------------------------------------

    def resolve_model(self, task: str = TASK_DEFAULT) -> str:
        """The alias to request for a task."""
        if task == TASK_TRIAGE:
            return self.settings.llm_triage_model
        if task == TASK_DEFAULT:
            return self.settings.llm_default_model
        raise ValueError(f"unknown task {task!r}; expected {TASK_DEFAULT!r} or {TASK_TRIAGE!r}")

    def spent_usd(self) -> Decimal:
        """What this audit has spent so far, summed from its logged calls."""
        total = self.session.execute(
            select(func.coalesce(func.sum(AgentRun.cost_usd), 0)).where(
                AgentRun.audit_id == self.audit_id
            )
        ).scalar_one()
        return as_usd(total)

    def budget_usd(self) -> Decimal | None:
        """The audit's budget, or None when it is unlimited."""
        audit = self._audit()
        return None if audit.budget_usd is None else Decimal(audit.budget_usd)

    def remaining_usd(self) -> Decimal | None:
        budget = self.budget_usd()
        if budget is None:
            return None
        return max(budget - self.spent_usd(), Decimal(0))

    def _check_budget(self) -> None:
        budget = self.budget_usd()
        if budget is None:
            return
        spent = self.spent_usd()
        if spent >= budget:
            raise BudgetExceeded(self.audit_id, budget, spent)

    # -- calls -------------------------------------------------------------

    def complete(
        self,
        agent: AgentName | str,
        messages: Sequence[Mapping[str, str]],
        *,
        task: str = TASK_DEFAULT,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResult:
        """Run one completion, logging every attempt and charging the audit.

        Raises :class:`BudgetExceeded` before calling when the audit is already
        out of budget, and :class:`LLMError` when every attempt on every
        candidate model failed.
        """
        agent_name = self._agent(agent)
        # Cheap check first so an exhausted audit never touches the network.
        self._check_budget()

        candidates = self._candidates(model or self.resolve_model(task))
        max_tokens = max_tokens or self.settings.llm_max_output_tokens
        temperature = self.settings.llm_temperature if temperature is None else temperature

        last_error: ProviderError | None = None
        attempts = 0
        for candidate in candidates:
            for attempt in range(1, self.settings.llm_max_attempts + 1):
                attempts += 1
                started = time.perf_counter()
                try:
                    completion = self.provider.complete(
                        model=candidate,
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=temperature,
                    )
                except ProviderError as exc:
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    self._record(
                        agent=agent_name,
                        model=candidate,
                        status=STATUS_FAILED,
                        latency_ms=latency_ms,
                    )
                    last_error = exc
                    if not exc.retryable:
                        break  # this request will not get better; try the next model
                    if attempt < self.settings.llm_max_attempts:
                        self._sleep(self._backoff(attempt))
                    continue

                latency_ms = int((time.perf_counter() - started) * 1000)
                cost = cost_usd(
                    candidate,
                    completion.input_tokens,
                    completion.output_tokens,
                    reported=completion.reported_cost_usd,
                    settings=self.settings,
                )
                self._record(
                    agent=agent_name,
                    model=candidate,
                    status=STATUS_SUCCEEDED,
                    input_tokens=completion.input_tokens,
                    output_tokens=completion.output_tokens,
                    cost=cost,
                    latency_ms=latency_ms,
                )
                return LLMResult(
                    text=completion.text,
                    model=candidate,
                    input_tokens=completion.input_tokens,
                    output_tokens=completion.output_tokens,
                    cost_usd=cost,
                    latency_ms=latency_ms,
                    attempts=attempts,
                    used_fallback=candidate != candidates[0],
                )

        raise LLMError(
            f"LLM call for agent {agent_name.value} failed after {attempts} attempt(s) on "
            f"{', '.join(candidates)}: {last_error}"
        )

    # -- internals ---------------------------------------------------------

    def _audit(self) -> Audit:
        audit = self.session.get(Audit, self.audit_id)
        if audit is None:
            raise LLMError(f"audit {self.audit_id} does not exist")
        return audit

    def _agent(self, agent: AgentName | str) -> AgentName:
        if isinstance(agent, AgentName):
            return agent
        try:
            return AgentName(agent)
        except ValueError as exc:
            allowed = ", ".join(member.value for member in AgentName)
            raise ValueError(f"unknown agent {agent!r}; expected one of {allowed}") from exc

    def _candidates(self, primary: str) -> list[str]:
        fallback = self.settings.llm_fallback_model
        if self.settings.llm_fallback_enabled and fallback and fallback != primary:
            return [primary, fallback]
        return [primary]

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff, capped; attempt is 1-based."""
        delay = self.settings.llm_retry_base_seconds * (2 ** (attempt - 1))
        return min(delay, self.settings.llm_retry_max_seconds)

    def _record(
        self,
        *,
        agent: AgentName,
        model: str,
        status: str,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost: Decimal | None = None,
        latency_ms: int | None = None,
    ) -> AgentRun:
        """Write the agent_runs row and keep audits.cost_usd in step with it."""
        run = AgentRun(
            audit_id=self.audit_id,
            agent=agent,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            status=status,
        )
        self.session.add(run)
        self.session.flush()
        audit = self.session.get(Audit, self.audit_id)
        if audit is not None:
            # Derived from the logged rows, so the display column cannot drift.
            audit.cost_usd = self.spent_usd()
        self.session.commit()
        return run


__all__ = [
    "BudgetExceeded",
    "ChatProvider",
    "Completion",
    "GatewayChatProvider",
    "LLMClient",
    "LLMError",
    "LLMResult",
    "ProviderError",
    "STATUS_FAILED",
    "STATUS_SUCCEEDED",
    "TASK_DEFAULT",
    "TASK_TRIAGE",
    "cost_usd",
    "get_provider",
    "price_for",
]
