"""Server-Sent Events for a live audit (step 3.7).

``GET /api/v1/audits/{id}/events`` sends:

    event: snapshot   once, immediately  - the full stage list, elapsed time, cost
    event: progress   on every stage change published by the workflow
    event: end        when the audit reaches a terminal status; the stream then closes
    : keepalive       every 15s of silence, so idle proxies do not drop the connection

The first event is the same shape as the rest, so a client that connects late (or
reconnects) needs no separate initial fetch.
"""

from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import get_current_user_allow_query, require_roles
from app.db.enums import UserRole
from app.db.models import Audit
from app.db.session import get_session
from app.progress import channel, snapshot

router = APIRouter(prefix="/api/v1", tags=["events"])

KEEPALIVE_SECONDS = 15.0


def sse_frame(event: str, data: dict) -> str:
    """One SSE frame. ``data`` is a single line of JSON, so no multi-line framing."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _stream(audit_id: uuid.UUID, first: dict):
    yield sse_frame("snapshot", first)
    if first["final"]:
        yield sse_frame("end", first)
        return

    import redis.asyncio as redis

    client = redis.from_url(settings.redis_url)
    pubsub = client.pubsub()
    await pubsub.subscribe(channel(audit_id))
    try:
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=KEEPALIVE_SECONDS
            )
            if message is None:
                yield ": keepalive\n\n"
                continue
            try:
                payload = json.loads(message["data"])
            except (TypeError, ValueError):
                continue
            yield sse_frame("progress", payload)
            if payload.get("final"):
                yield sse_frame("end", payload)
                return
    finally:
        try:
            await pubsub.unsubscribe(channel(audit_id))
        finally:
            await pubsub.aclose()
            await client.aclose()


@router.get(
    "/audits/{audit_id}/events",
    summary="Live stage progress for an audit (Server-Sent Events)",
    response_class=StreamingResponse,
)
def audit_events(
    audit_id: uuid.UUID,
    # EventSource cannot set headers, so this route alone also accepts ?token=. The
    # role check is the same one every other audit route applies.
    user=Depends(require_roles(UserRole.ANALYST, user_dep=get_current_user_allow_query)),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    audit = session.get(Audit, audit_id)
    if audit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="audit not found")

    first = snapshot(session, audit_id)
    return StreamingResponse(
        _stream(audit_id, first),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # nginx buffers responses by default, which would hold the frames back.
            "X-Accel-Buffering": "no",
        },
    )
