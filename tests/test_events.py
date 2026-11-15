"""Step 3.7: live progress over Server-Sent Events.

These tests use real tokens (no dependency override): the endpoint accepts the token
from the query string because that is the only thing ``EventSource`` can send, and the
role check has to happen on that path too.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.auth import access_token
from app.core.config import settings
from app.db.enums import (
    AgentName,
    AuditStatus,
    Category,
    EvidenceKind,
    FindingOrigin,
    Severity,
    SourceType,
    Stage,
    UserRole,
    VerificationStatus,
    Verdict,
)
from app.db.models import AgentRun, Audit, Evidence, Finding, Repository, Review, User
from app.db.session import get_session_factory
from app.main import app
from app.progress import channel, snapshot, stage_states
from app.workflow.graph import postgres_checkpointer, start_or_resume
from app.workflow.nodes import WorkflowServices

pytestmark = pytest.mark.real_auth


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@dataclass
class Account:
    id: uuid.UUID
    role: UserRole

    @property
    def token(self) -> str:
        return access_token(self.id, self.role)


@pytest.fixture(scope="module")
def analyst():
    session = get_session_factory()()
    user = User(
        id=uuid.uuid4(),
        email=f"pytest-events-analyst-{uuid.uuid4().hex[:6]}@example.invalid",
        password_hash="not-used-by-these-tests",
        role=UserRole.ANALYST,
        is_active=True,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    account = Account(id=user.id, role=user.role)
    yield account
    session.execute(text("ALTER TABLE review_decisions DISABLE TRIGGER USER"))
    session.delete(session.get(User, account.id))
    session.commit()
    session.execute(text("ALTER TABLE review_decisions ENABLE TRIGGER USER"))
    session.commit()
    session.close()


@pytest.fixture(scope="module")
def reviewer():
    session = get_session_factory()()
    user = User(
        id=uuid.uuid4(),
        email=f"pytest-events-reviewer-{uuid.uuid4().hex[:6]}@example.invalid",
        password_hash="not-used-by-these-tests",
        role=UserRole.REVIEWER,
        is_active=True,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    account = Account(id=user.id, role=user.role)
    yield account
    session.execute(text("ALTER TABLE review_decisions DISABLE TRIGGER USER"))
    session.delete(session.get(User, account.id))
    session.commit()
    session.execute(text("ALTER TABLE review_decisions ENABLE TRIGGER USER"))
    session.commit()
    session.close()


@pytest.fixture
def audit(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("print('hello')\n", encoding="utf-8")
    session = get_session_factory()()
    repository = Repository(
        name=f"pytest-events-{uuid.uuid4().hex[:8]}",
        source_type=SourceType.ZIP,
        storage_path=str(tmp_path),
    )
    session.add(repository)
    session.commit()
    session.refresh(repository)
    row = Audit(repository_id=repository.id, status=AuditStatus.QUEUED)
    session.add(row)
    session.commit()
    session.refresh(row)
    audit_id = row.id
    session.close()

    yield audit_id

    session = get_session_factory()()
    session.execute(text("ALTER TABLE review_decisions DISABLE TRIGGER USER"))
    session.delete(session.get(Audit, audit_id))
    session.commit()
    session.execute(text("ALTER TABLE review_decisions ENABLE TRIGGER USER"))
    session.delete(session.get(Repository, repository.id))
    session.commit()
    session.close()


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def set_status(audit_id: uuid.UUID, *, status: AuditStatus, stage: Stage | None) -> None:
    session = get_session_factory()()
    audit = session.get(Audit, audit_id)
    audit.status = status
    audit.current_stage = stage
    session.commit()
    session.close()


# --------------------------------------------------------------------------- #
# stage states
# --------------------------------------------------------------------------- #


def test_a_queued_audit_has_every_stage_pending():
    states = stage_states("queued", None, has_review=False)

    assert set(states.values()) == {"pending"}
    assert len(states) == len(Stage)


def test_a_running_audit_divides_the_stages_at_the_current_one():
    states = stage_states("running", "agents", has_review=False)

    assert states["intake"] == states["index"] == states["scanners"] == "done"
    assert states["agents"] == "running"
    assert states["validation"] == states["review"] == states["report"] == "pending"


def test_a_completed_audit_without_a_review_shows_the_gate_as_skipped():
    states = stage_states("completed", "report", has_review=False)

    assert set(states.values()) == {"done", "skipped"}
    assert states["review"] == "skipped"
    assert states["report"] == "done"


def test_a_completed_audit_with_a_review_shows_it_done():
    states = stage_states("completed", "report", has_review=True)

    assert set(states.values()) == {"done"}


def test_a_failed_audit_marks_the_stage_it_died_in():
    states = stage_states("failed", "scanners", has_review=False)

    assert states["scanners"] == "failed"
    assert states["index"] == "done"
    assert states["agents"] == "pending"


def test_an_audit_waiting_for_a_human_shows_the_review_running():
    states = stage_states("awaiting_review", "report", has_review=True)

    assert states["review"] == "done"
    assert states["report"] == "running"


# --------------------------------------------------------------------------- #
# the snapshot
# --------------------------------------------------------------------------- #


def test_the_snapshot_carries_stages_elapsed_time_and_cost(audit):
    session = get_session_factory()()
    session.add(
        AgentRun(
            audit_id=audit,
            agent=AgentName.SECURITY,
            model="agent-default",
            cost_usd=Decimal("0.0125"),
            status="ok",
        )
    )
    session.add(
        AgentRun(
            audit_id=audit,
            agent=AgentName.ARCHITECTURE,
            model="agent-default",
            cost_usd=Decimal("0.0075"),
            status="ok",
        )
    )
    result = session.get(Audit, audit)
    result.status = AuditStatus.RUNNING
    result.current_stage = Stage.AGENTS
    from datetime import datetime, timedelta, timezone

    result.started_at = datetime.now(timezone.utc) - timedelta(seconds=3)
    session.commit()
    try:
        payload = snapshot(session, audit)
    finally:
        session.close()

    assert payload["audit_id"] == str(audit)
    assert payload["status"] == "running"
    assert payload["current_stage"] == "agents"
    assert payload["stages"]["agents"] == "running"
    assert payload["cost_usd"] == pytest.approx(0.02)
    assert payload["elapsed_ms"] >= 3_000
    assert payload["final"] is False


def test_a_terminal_audit_is_marked_final(audit):
    set_status(audit, status=AuditStatus.COMPLETED, stage=Stage.REPORT)
    session = get_session_factory()()
    try:
        assert snapshot(session, audit)["final"] is True
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# authentication and roles
# --------------------------------------------------------------------------- #


def test_the_stream_needs_a_token(client, audit):
    response = client.get(f"/api/v1/audits/{audit}/events")

    assert response.status_code == 401


def test_a_reviewer_cannot_read_the_progress_stream(client, audit, reviewer):
    response = client.get(
        f"/api/v1/audits/{audit}/events", params={"token": reviewer.token}
    )

    assert response.status_code == 403


def test_an_invalid_query_token_is_401(client, audit):
    response = client.get(f"/api/v1/audits/{audit}/events", params={"token": "nope"})

    assert response.status_code == 401


def test_an_unknown_audit_is_404(client, analyst):
    response = client.get(
        f"/api/v1/audits/{uuid.uuid4()}/events", params={"token": analyst.token}
    )

    assert response.status_code == 404


def test_the_header_works_as_well_as_the_query_string(client, analyst, audit):
    with client.stream(
        "GET",
        f"/api/v1/audits/{audit}/events",
        headers={"Authorization": f"Bearer {analyst.token}"},
    ) as response:
        assert response.status_code == 200
        first = next(response.iter_lines())


# --------------------------------------------------------------------------- #
# the stream
# --------------------------------------------------------------------------- #


def parse_frames(lines, *, stop_at: int = 10) -> list[tuple[str, dict]]:
    """Collect frames until the end event arrives (or ``stop_at`` frames)."""
    frames: list[tuple[str, dict]] = []
    event = None
    for line in lines:
        if line.startswith("event: "):
            event = line[len("event: ") :]
        elif line.startswith("data: ") and event is not None:
            frames.append((event, json.loads(line[len("data: ") :])))
            ended = event == "end"
            event = None
            if ended or len(frames) >= stop_at:
                break
    return frames


def progress_payload(audit_id: uuid.UUID, *, final: bool = False) -> dict:
    return {
        "type": "progress",
        "audit_id": str(audit_id),
        "status": "completed" if final else "running",
        "current_stage": "agents",
        "stages": stage_states(
            "completed" if final else "running", "agents", has_review=False
        ),
        "elapsed_ms": 4_200,
        "cost_usd": 0.031,
        "final": final,
        "at": "2026-11-15T10:00:00+00:00",
    }


def publish_later(audit_id: uuid.UUID, payload: dict, delay: float = 0.3) -> None:
    def send() -> None:
        client = redis.Redis.from_url(settings.redis_url)
        client.publish(channel(audit_id), json.dumps(payload))
        client.close()

    timer = threading.Timer(delay, send)
    timer.start()


def test_the_stream_sends_a_snapshot_then_live_events_then_ends(client, analyst, audit):
    set_status(audit, status=AuditStatus.RUNNING, stage=Stage.INDEX)
    publish_later(audit, progress_payload(audit))
    publish_later(audit, progress_payload(audit, final=True), delay=0.6)

    with client.stream(
        "GET", f"/api/v1/audits/{audit}/events", params={"token": analyst.token}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"].startswith("no-cache")
        frames = parse_frames(response.iter_lines())

    names = [name for name, _ in frames]
    assert names == ["snapshot", "progress", "progress", "end"]

    snap = frames[0][1]
    assert snap["current_stage"] == "index"
    assert snap["stages"]["index"] == "running"
    assert snap["stages"]["intake"] == "done"

    live = frames[1][1]
    assert live["cost_usd"] == pytest.approx(0.031)
    assert live["elapsed_ms"] == 4_200
    assert frames[2][1]["final"] is True


def test_a_finished_audit_ends_the_stream_on_the_first_event(client, analyst, audit):
    set_status(audit, status=AuditStatus.COMPLETED, stage=Stage.REPORT)

    with client.stream(
        "GET", f"/api/v1/audits/{audit}/events", params={"token": analyst.token}
    ) as response:
        frames = parse_frames(response.iter_lines())

    assert [name for name, _ in frames] == ["snapshot", "end"]


def test_the_workflow_publishes_a_stage_event_per_stage(audit):
    """The graph's own commits are what the tracker sees, not a test-only publisher."""
    client = redis.Redis.from_url(settings.redis_url)
    pubsub = client.pubsub()
    pubsub.subscribe(channel(audit))
    try:
        # redis-py sends SUBSCRIBE lazily; without this the run's first events would be
        # published before the subscription reaches the server.
        pubsub.get_message(timeout=0.1)
        run_with_stubs(audit)
        time.sleep(0.2)
        received = []
        while True:
            message = pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)
            if message is None:
                break
            received.append(json.loads(message["data"]))
    finally:
        pubsub.unsubscribe(channel(audit))
        pubsub.close()
        client.close()

    stages = [payload["current_stage"] for payload in received]
    assert "intake" in stages
    assert "index" in stages
    assert "scanners" in stages
    assert "agents" in stages
    assert "validation" in stages
    # The last event describes the finished run, so the tracker can stop.
    assert received[-1]["final"] is True
    assert received[-1]["status"] == "completed"
    assert received[-1]["stages"]["review"] == "skipped"


@dataclass
class Recorder:
    validation: list = field(default_factory=list)


def run_with_stubs(audit_id: uuid.UUID) -> None:
    def index(session, audit, snapshot_path):
        return SimpleNamespace(included=1, excluded=0)

    def scanners(audit, snapshot_path, session_factory=None):
        return SimpleNamespace(failed=())

    def agents(session, audit):
        return SimpleNamespace(errors=(), accepted=0, calls=0)

    def validate(session, audit, semantic=True):
        return SimpleNamespace(checked=0, verified=0, rejected=0, unresolved=0)

    services = WorkflowServices(
        session_factory=get_session_factory(),
        index=index,
        scanners=scanners,
        agents=agents,
        validation=validate,
    )
    with postgres_checkpointer() as saver:
        start_or_resume(audit_id, services=services, checkpointer=saver)


def test_publishing_survives_redis_being_unreachable(monkeypatch):
    from app import progress

    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:6399/0")

    published = progress.publish(
        {
            "type": "progress",
            "audit_id": str(uuid.uuid4()),
            "status": "running",
            "current_stage": "intake",
            "stages": {},
            "elapsed_ms": 0,
            "cost_usd": 0.0,
            "final": False,
            "at": "2026-11-15T10:00:00+00:00",
        }
    )

    assert published is False
