"""Per-node retry policies (step 3.2).

A retry here is not the model client's retry: ``LLMClient`` already retries a failed
call, backs off and falls back to another model. This layer retries *the node* - a
whole scanners stage, a whole agents stage - and only for the failures the
technology can actually recover from by running it again.

``BudgetExceeded`` is never retried. Spending more money to discover the budget is
gone is the one retry that cannot help.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from app.llm.client import BudgetExceeded

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    """How many times a node is attempted, and how long to wait between tries."""

    attempts: int = 1
    delay_seconds: float = 0.0
    backoff: float = 2.0
    retry_on: tuple[type[BaseException], ...] = (Exception,)

    def should_retry(self, exc: BaseException) -> bool:
        return not isinstance(exc, BudgetExceeded) and isinstance(exc, self.retry_on)

    def wait(self, attempt: int) -> float:
        return min(self.delay_seconds * (self.backoff ** (attempt - 1)), 30.0)


#: One policy per node. Scanner and model failures retry; intake, routing and
#: bookkeeping do not - a missing snapshot does not appear by being asked again.
RETRY_POLICIES: dict[str, RetryPolicy] = {
    "intake": RetryPolicy(attempts=1),
    "index": RetryPolicy(attempts=2, delay_seconds=0.05),
    "scanners": RetryPolicy(attempts=2, delay_seconds=0.05),
    "agents": RetryPolicy(attempts=2, delay_seconds=0.05),
    "validation": RetryPolicy(attempts=2, delay_seconds=0.05),
    "risk": RetryPolicy(attempts=1),
    "route": RetryPolicy(attempts=1),
    "finalize": RetryPolicy(attempts=1),
    "review": RetryPolicy(attempts=1),
    "report": RetryPolicy(attempts=1),
}


def run_with_retry(node: str, work: Callable[[], T], policy: RetryPolicy | None = None) -> T:
    """Call ``work`` under the node's policy, re-raising the last failure."""
    chosen = policy or RETRY_POLICIES.get(node, RetryPolicy())
    attempt = 0
    while True:
        attempt += 1
        try:
            return work()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            if attempt >= chosen.attempts or not chosen.should_retry(exc):
                raise
            wait = chosen.wait(attempt)
            logger.warning(
                "%s node failed (attempt %d/%d): %s%s",
                node,
                attempt,
                chosen.attempts,
                type(exc).__name__,
                f"; retrying in {wait:.2f}s" if wait else "; retrying",
            )
            if wait:
                time.sleep(wait)


__all__ = ["RETRY_POLICIES", "RetryPolicy", "run_with_retry"]
