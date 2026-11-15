"""Live progress for an audit (step 3.7).

The workflow publishes; ``GET /api/v1/audits/{id}/events`` subscribes and relays the
messages as Server-Sent Events. Redis pub/sub is the wire because the workflow runs in
a Celery worker in another process (and, in a deployment, on another host) and the API
process must see its events.

What is published is a *snapshot per stage change*, not an ordered sequence of deltas.
A reader that misses a message - a browser tab that slept, a reconnect after a dropped
connection - is correct again on the next one, and the endpoint's first event is the
same snapshot, so connecting late still shows the whole stage list, the elapsed time
and the cost so far.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.enums import AuditStatus, Stage
from app.db.models import AgentRun, Audit, Review

logger = logging.getLogger(__name__)

#: The order the stages run in, and the order the tracker displays.
STAGE_ORDER: tuple[str, ...] = tuple(stage.value for stage in Stage)

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"

#: Statuses after which nothing more will happen to the audit.
TERMINAL_STATUSES = {AuditStatus.COMPLETED.value, AuditStatus.FAILED.value}


def channel(audit_id: str | uuid.UUID) -> str:
    return f"progress:audit:{audit_id}"


def stage_states(status: str, current: str | None, has_review: bool) -> dict[str, str]:
    """Per-stage state for the tracker, from the audit's status and current stage.

    ``skipped`` exists for the review stage: a run whose risk never reached critical
    never pauses, and showing "pending" forever for a gate that will not be asked is a
    lie the operator would have to interpret.
    """
    states = {name: PENDING for name in STAGE_ORDER}
    if status == AuditStatus.QUEUED.value or current is None:
        return states

    current_index = STAGE_ORDER.index(current) if current in STAGE_ORDER else -1
    if current_index < 0:
        return states

    for name in STAGE_ORDER[:current_index]:
        states[name] = DONE

    if status == AuditStatus.COMPLETED.value:
        states[current] = DONE
        for name in STAGE_ORDER[current_index + 1 :]:
            states[name] = DONE
    elif status == AuditStatus.FAILED.value:
        states[current] = FAILED
    else:
        # running, awaiting_review or needs_evidence: the current stage is in progress.
        states[current] = RUNNING

    if not has_review and states.get(Stage.REVIEW.value) == DONE:
        states[Stage.REVIEW.value] = SKIPPED
    return states


def snapshot(session: Session, audit_id: str | uuid.UUID) -> dict:
    """The current picture of an audit: stages, elapsed time and cost so far."""
    audit = session.get(Audit, uuid.UUID(str(audit_id)))
    if audit is None:
        raise LookupError(f"audit {audit_id} does not exist")

    has_review = (
        session.execute(select(Review.id).where(Review.audit_id == audit.id).limit(1)).first()
        is not None
    )
    cost = session.execute(
        select(func.coalesce(func.sum(AgentRun.cost_usd), 0)).where(AgentRun.audit_id == audit.id)
    ).scalar()
    now = datetime.now(timezone.utc)
    started = audit.started_at
    elapsed_ms = int((now - started).total_seconds() * 1000) if started else 0

    status = audit.status.value
    return {
        "type": "progress",
        "audit_id": str(audit.id),
        "status": status,
        "current_stage": audit.current_stage.value if audit.current_stage else None,
        "stages": stage_states(status, audit.current_stage.value if audit.current_stage else None, has_review),
        "elapsed_ms": max(elapsed_ms, 0),
        "cost_usd": float(cost or 0),
        "final": status in TERMINAL_STATUSES,
        "at": now.isoformat(),
    }


def _client():
    """A short-lived publisher. Progress must never be the reason a run fails."""
    import redis

    return redis.Redis.from_url(settings.redis_url, socket_timeout=1.0)


def publish(payload: dict) -> bool:
    """Publish one snapshot; False if Redis refused (the run continues regardless)."""
    try:
        with _client() as client:
            client.publish(channel(payload["audit_id"]), json.dumps(payload))
        return True
    except Exception as exc:  # noqa: BLE001 - progress is best-effort by design
        logger.warning("could not publish progress for %s: %s", payload.get("audit_id"), exc)
        return False


def publish_progress(session: Session, audit_id: str | uuid.UUID) -> dict | None:
    """Publish the audit's current snapshot; the caller has already committed."""
    try:
        payload = snapshot(session, audit_id)
    except LookupError:
        return None
    publish(payload)
    return payload


__all__ = [
    "STAGE_ORDER",
    "channel",
    "snapshot",
    "publish",
    "publish_progress",
    "stage_states",
]
