"""Observability: Langfuse tracing and the audit-level cost record (step 3.8).

An audit is one Langfuse trace. Its id is derived from the audit id rather than stored,
so the API process, the Celery worker and a resumed run all address the same trace
without a lookup, and ``agent_runs.trace_id`` carries the same value.

Tracing is optional and best-effort: with no ``LANGFUSE_HOST``/keys every method is a
no-op and a run behaves exactly as before. A tracing failure is logged and swallowed -
an audit that cannot be observed should still produce its report.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)


def _usage(input_tokens: int, output_tokens: int):
    """The v2 SDK's usage object; the v2 server ignores ``usage_details``."""
    try:
        from langfuse.model import ModelUsage

        return ModelUsage(input=input_tokens, output=output_tokens, unit="TOKENS")
    except Exception:  # noqa: BLE001 - older SDKs accept a plain mapping
        return {"input": input_tokens, "output": output_tokens, "unit": "TOKENS"}


def trace_id_for(audit_id: str | uuid.UUID) -> str:
    """The Langfuse trace id for an audit: its uuid, without dashes."""
    return uuid.UUID(str(audit_id)).hex


class Tracer:
    """The slice of the Langfuse client this application uses."""

    def __init__(self, client: Any | None = None) -> None:
        self._client = client
        self.enabled = client is not None

    # -- lifecycle ---------------------------------------------------------

    def start_audit(
        self,
        *,
        audit_id: str | uuid.UUID,
        repository: str | None = None,
        started_by: str | None = None,
        budget_usd: Decimal | None = None,
    ) -> str:
        """Open the audit's trace. Idempotent: called again on a resumed run."""
        trace_id = trace_id_for(audit_id)
        if not self.enabled:
            return trace_id
        try:
            self._client.trace(
                id=trace_id,
                name="audit",
                session_id=str(audit_id),
                user_id=started_by,
                metadata={
                    "audit_id": str(audit_id),
                    "repository": repository,
                    "budget_usd": str(budget_usd) if budget_usd is not None else None,
                },
            )
        except Exception as exc:  # noqa: BLE001 - tracing must never fail a run
            logger.warning("langfuse: could not open trace %s: %s", trace_id, exc)
        return trace_id

    def end_audit(
        self,
        *,
        audit_id: str | uuid.UUID,
        status: str,
        cost_usd: Decimal | float,
        incomplete_reason: str | None = None,
    ) -> None:
        """Close the trace with the outcome the audit actually reached."""
        if not self.enabled:
            return
        trace_id = trace_id_for(audit_id)
        try:
            self._client.trace(
                id=trace_id,
                name="audit",
                session_id=str(audit_id),
                output={"status": status, "cost_usd": str(cost_usd)},
                metadata={
                    "audit_id": str(audit_id),
                    "status": status,
                    "cost_usd": str(cost_usd),
                    "incomplete_reason": incomplete_reason,
                },
            )
            self.flush()
        except Exception as exc:  # noqa: BLE001
            logger.warning("langfuse: could not close trace %s: %s", trace_id, exc)

    # -- one model call ----------------------------------------------------

    def generation(
        self,
        *,
        audit_id: str | uuid.UUID,
        agent: str,
        model: str,
        status: str,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd: Decimal | None = None,
        latency_ms: int | None = None,
        error: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Record one attempt: model, tokens, cost, latency and the outcome."""
        if not self.enabled:
            return
        trace_id = trace_id_for(audit_id)
        end = datetime.now(timezone.utc)
        start = end - timedelta(milliseconds=latency_ms or 0)
        usage = _usage(input_tokens or 0, output_tokens or 0)
        try:
            self._client.generation(
                trace_id=trace_id,
                name=agent,
                model=model,
                start_time=start,
                end_time=end,
                input=metadata.get("summary") if metadata else None,
                output=error or status,
                # Langfuse v2 computes the displayed cost from the model registry, so
                # the authoritative figure - what the client actually charged the
                # audit - travels in metadata as well. The two agree when the model's
                # registered prices match the provider's.
                usage=usage,
                level="ERROR" if status != "succeeded" else "DEFAULT",
                status_message=error,
                metadata={
                    "audit_id": str(audit_id),
                    "agent": agent,
                    "attempt_status": status,
                    "latency_ms": latency_ms,
                    "cost_usd": str(cost_usd) if cost_usd is not None else None,
                    **(metadata or {}),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("langfuse: could not record generation for %s: %s", trace_id, exc)

    def flush(self) -> None:
        if not self.enabled:
            return
        try:
            self._client.flush()
        except Exception as exc:  # noqa: BLE001
            logger.warning("langfuse: flush failed: %s", exc)


@lru_cache(maxsize=1)
def get_tracer() -> Tracer:
    """The process's tracer: a real client when configured, otherwise a no-op."""
    host = (settings.langfuse_host or "").strip()
    public_key = settings.langfuse_public_key.get_secret_value() if settings.langfuse_public_key else ""
    secret_key = settings.langfuse_secret_key.get_secret_value() if settings.langfuse_secret_key else ""
    if not (host and public_key and secret_key):
        return Tracer(None)
    try:
        from langfuse import Langfuse

        client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
            # The worker is long-lived; flush explicitly at the end of each audit.
            flush_at=20,
        )
        return Tracer(client)
    except Exception as exc:  # noqa: BLE001 - a broken tracer must not stop audits
        logger.warning("langfuse: tracing disabled, client could not start: %s", exc)
        return Tracer(None)


__all__ = ["Tracer", "get_tracer", "trace_id_for"]
