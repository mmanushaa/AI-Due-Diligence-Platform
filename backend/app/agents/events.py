"""What a crew did while it worked, written down as it happened (step 2.6e).

``agent_runs`` answers "did a call happen, what did it cost". It cannot answer
"which tool did the model ask for", "what did the guardrail refuse", or "what did
the model actually write when it was refused" — and those three are exactly what a
failed stage has to be diagnosed from. Before this module the answers lived in a
``list[str]`` that was thrown away and in a ``logger.debug`` line that nothing
keeps.

Three kinds of row are recorded:

    tool_call          the name, the arguments, and how many characters came back
    guardrail_failure  the reply that was refused, the feedback, and the attempt
    final_output       the raw text each task ended with

Two rules hold for all of them:

* **Masked.** Every string goes through :func:`app.scanners.common.redact_text`,
  the same masker the scanners use, before it is stored. A model can echo a
  credential it read out of the repository, so the raw reply is never written
  verbatim even though it is the thing worth keeping.
* **Never fatal.** Recording is instrumentation: a failure here logs and gets out
  of the way rather than failing the audit it was meant to describe.

The rows join the caller's transaction (``flush``, no ``commit``): the stage that
owns the session decides when work is durable, exactly as it does for findings.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.db.enums import AgentEventType, AgentName
from app.db.models import AgentEvent
from app.scanners.common import redact_text

logger = logging.getLogger(__name__)

#: A reply is worth keeping whole up to here; past it the head is what diagnoses.
MAX_PAYLOAD_CHARS = 8_000


def masked(value: Any) -> Any:
    """The same secret masker the scanners use, applied to anything we keep.

    Walks dicts and lists so a tool's arguments are masked field by field rather
    than by stringifying the whole structure and masking the result.
    """
    if isinstance(value, str):
        return redact_text(value)[:MAX_PAYLOAD_CHARS]
    if isinstance(value, dict):
        return {str(key): masked(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [masked(item) for item in value]
    return value


def record_event(
    session: Session,
    audit_id: uuid.UUID,
    *,
    agent: AgentName,
    event_type: AgentEventType,
    tool_name: str | None = None,
    attempt: int | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Add one row and flush it, without deciding when the audit commits."""
    try:
        session.add(
            AgentEvent(
                audit_id=audit_id,
                agent=agent,
                event_type=event_type,
                tool_name=tool_name,
                attempt=attempt,
                payload=masked(payload or {}),
            )
        )
        session.flush()
    except Exception as exc:  # noqa: BLE001 - instrumentation must not fail an audit
        logger.warning("could not record %s for audit %s: %s", event_type.value, audit_id, exc)
        try:
            # A failed flush leaves the session unusable; recover it so the stage
            # that owns it can still finish. Nothing else is pending mid-crew.
            session.rollback()
        except Exception:  # noqa: BLE001 - a closed session has nothing to recover
            pass


def record_tool_call(
    session: Session,
    audit_id: uuid.UUID,
    *,
    agent: AgentName,
    tool_name: str,
    arguments: dict[str, Any],
    result_chars: int,
) -> None:
    """One tool the model asked for, with its arguments and the size of the answer.

    The result itself is not stored: it is repository text that the audit already
    has, and its size is what tells an investigation apart from a stub answer.
    """
    record_event(
        session,
        audit_id,
        agent=agent,
        event_type=AgentEventType.TOOL_CALL,
        tool_name=tool_name,
        payload={"arguments": arguments, "result_chars": result_chars},
    )


def record_guardrail_failure(
    session: Session,
    audit_id: uuid.UUID,
    *,
    agent: AgentName,
    attempt: int,
    raw_reply: str,
    reason: str,
) -> None:
    """A reply a guardrail refused, with the text that was refused."""
    record_event(
        session,
        audit_id,
        agent=agent,
        event_type=AgentEventType.GUARDRAIL_FAILURE,
        attempt=attempt,
        payload={"raw": raw_reply, "reason": reason},
    )


def record_final_output(
    session: Session,
    audit_id: uuid.UUID,
    *,
    agent: AgentName,
    task: str,
    raw: str,
) -> None:
    """The raw text one task ended with, whatever the stage then made of it."""
    record_event(
        session,
        audit_id,
        agent=agent,
        event_type=AgentEventType.FINAL_OUTPUT,
        payload={"task": task, "raw": raw},
    )


def record_final_outputs(
    session: Session,
    audit_id: uuid.UUID,
    *,
    agent: AgentName,
    outputs: dict[str, str],
) -> None:
    """Every task's final text, in task order."""
    for task, raw in outputs.items():
        record_final_output(session, audit_id, agent=agent, task=task, raw=raw)
