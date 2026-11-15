"""Step 3.5: the audit trail, and the two tables that cannot be changed.

The point of the step is not that a service writes rows; it is that the rows cannot
be rewritten afterwards. So the tests attempt the rewrite three ways - through SQL,
through the ORM the application uses, and as the application's own database role -
and require every attempt to fail.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from app.api import reviews as reviews_api
from app.core.audit import record
from app.db.enums import (
    AuditStatus,
    Category,
    EvidenceKind,
    FindingOrigin,
    Severity,
    SourceType,
    VerificationStatus,
    Verdict,
)
from app.db.models import Audit, AuditLog, Evidence, Finding, Repository
from app.db.session import get_engine, get_session_factory
from app.main import app
from app.workflow.graph import pending_nodes, postgres_checkpointer, start_or_resume
from app.workflow.nodes import WorkflowServices

IMMUTABLE = ("audit_log", "review_decisions")
APP_ROLE = "due_diligence_app"


@pytest.fixture
def audit(tmp_path):
    """An audit with one critical finding, over a small repository."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("print('hello')\n", encoding="utf-8")

    session = get_session_factory()()
    repository = Repository(
        name=f"pytest-trail-{uuid.uuid4().hex[:8]}",
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

    finding = Finding(
        audit_id=audit_id,
        category=Category.SECURITY,
        title="Export route reaches another tenant's report",
        severity=Severity.CRITICAL,
        confidence=Decimal("0.90"),
        origin=FindingOrigin.SCANNER,
        source_tool="semgrep",
        verification_status=VerificationStatus.SCANNER_CONFIRMED,
        verdict=Verdict.ISSUE,
        reasoning="the handler takes the report id from the request",
        fingerprint=uuid.uuid4().hex,
    )
    session.add(finding)
    session.flush()
    session.add(
        Evidence(
            finding_id=finding.id,
            file_path="app/main.py",
            line_start=1,
            line_end=1,
            snippet="print('hello')",
            kind=EvidenceKind.PRIMARY,
        )
    )
    session.commit()
    session.close()

    yield audit_id

    # Deleting the audit cascades into review_decisions, which is append-only. The
    # trigger is what a test database has to step around; no product code may.
    session = get_session_factory()()
    session.execute(text("ALTER TABLE review_decisions DISABLE TRIGGER USER"))
    session.delete(session.get(Audit, audit_id))
    session.commit()
    session.execute(text("ALTER TABLE review_decisions ENABLE TRIGGER USER"))
    session.delete(session.get(Repository, repository.id))
    session.commit()
    session.close()


def one_log_row() -> uuid.UUID:
    session = get_session_factory()()
    try:
        row = record(
            session,
            actor="pytest",
            action="test_action",
            object_type="audit",
            object_id=uuid.uuid4(),
            before={"status": "before"},
            after={"status": "after"},
        )
        session.commit()
        return row.id
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# the service
# --------------------------------------------------------------------------- #


def test_the_service_records_actor_action_object_and_both_states():
    session = get_session_factory()()
    object_id = uuid.uuid4()
    try:
        record(
            session,
            actor="reviewer-1",
            action="review_approved",
            object_type="review",
            object_id=object_id,
            before={"status": "pending", "level": "critical"},
            after={"status": "approved", "level": "critical"},
        )
        session.commit()
        row = session.execute(
            select(AuditLog).where(AuditLog.object_id == str(object_id))
        ).scalars().first()
    finally:
        session.close()

    assert row.actor == "reviewer-1"
    assert row.action == "review_approved"
    assert row.object_type == "review"
    assert row.before == {"status": "pending", "level": "critical"}
    assert row.after == {"status": "approved", "level": "critical"}


# --------------------------------------------------------------------------- #
# the tables are append-only: SQL
# --------------------------------------------------------------------------- #


#: A column each table actually has, for the UPDATE attempt.
COLUMN = {"audit_log": "actor", "review_decisions": "reason"}


@pytest.mark.parametrize("table", IMMUTABLE)
def test_sql_update_is_refused(table):
    one_log_row() if table == "audit_log" else None
    session = get_session_factory()()
    try:
        with pytest.raises(DBAPIError) as excinfo:
            session.execute(text(f"UPDATE {table} SET {COLUMN[table]} = 'tampered'"))
            session.commit()
        session.rollback()
    finally:
        session.close()

    assert "append-only" in str(excinfo.value)


@pytest.mark.parametrize("table", IMMUTABLE)
def test_sql_delete_is_refused(table):
    one_log_row() if table == "audit_log" else None
    session = get_session_factory()()
    try:
        with pytest.raises(DBAPIError) as excinfo:
            session.execute(text(f"DELETE FROM {table}"))
            session.commit()
        session.rollback()
    finally:
        session.close()

    assert "append-only" in str(excinfo.value)


def test_review_decisions_rows_survive_a_delete_attempt(audit):
    """A real decision row, not just an empty table."""
    session = get_session_factory()()
    try:
        before = session.execute(text("SELECT count(*) FROM review_decisions")).scalar()
        with pytest.raises(DBAPIError):
            session.execute(text("DELETE FROM review_decisions"))
            session.commit()
        session.rollback()
        after = session.execute(text("SELECT count(*) FROM review_decisions")).scalar()
    finally:
        session.close()

    assert after == before


# --------------------------------------------------------------------------- #
# the tables are append-only: the ORM the application uses
# --------------------------------------------------------------------------- #


def test_the_application_cannot_update_a_trail_row():
    row_id = one_log_row()
    session = get_session_factory()()
    try:
        row = session.get(AuditLog, row_id)
        row.actor = "tampered"
        with pytest.raises(SQLAlchemyError) as excinfo:
            session.commit()
        session.rollback()
    finally:
        session.close()

    assert "append-only" in str(excinfo.value)


def test_the_application_cannot_delete_a_trail_row():
    row_id = one_log_row()
    session = get_session_factory()()
    try:
        row = session.get(AuditLog, row_id)
        session.delete(row)
        with pytest.raises(SQLAlchemyError) as excinfo:
            session.commit()
        session.rollback()
    finally:
        session.close()

    assert "append-only" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# the tables are append-only: the application's database role
# --------------------------------------------------------------------------- #


def test_the_application_role_can_append_and_read_but_not_change():
    engine = get_engine()
    with engine.connect() as connection:
        try:
            connection.execute(text(f'SET ROLE "{APP_ROLE}"'))
            granted = connection.execute(
                text("SELECT has_table_privilege(current_user, 'audit_log', 'INSERT')")
            ).scalar()
            update_right = connection.execute(
                text("SELECT has_table_privilege(current_user, 'audit_log', 'UPDATE')")
            ).scalar()
            delete_right = connection.execute(
                text("SELECT has_table_privilege(current_user, 'audit_log', 'DELETE')")
            ).scalar()
            connection.rollback()
        finally:
            # SET ROLE outlives the transaction and the pooled connection is reused.
            connection.rollback()
            connection.execute(text("RESET ROLE"))
            connection.commit()

    assert granted is True
    assert update_right is False
    assert delete_right is False


@pytest.mark.parametrize("statement", ["UPDATE audit_log SET actor='x'", "DELETE FROM audit_log"])
def test_the_application_role_is_refused_by_the_database(statement):
    engine = get_engine()
    with engine.connect() as connection:
        try:
            connection.execute(text(f'SET ROLE "{APP_ROLE}"'))
            with pytest.raises(DBAPIError) as excinfo:
                connection.execute(text(statement))
                connection.commit()
            connection.rollback()
        finally:
            connection.rollback()
            connection.execute(text("RESET ROLE"))
            connection.commit()

    assert "permission denied" in str(excinfo.value).lower()


def test_the_application_role_can_insert():
    engine = get_engine()
    marker = f"role-insert-{uuid.uuid4().hex[:8]}"
    with engine.connect() as connection:
        try:
            connection.execute(text(f'SET ROLE "{APP_ROLE}"'))
            connection.execute(
                text(
                    "INSERT INTO audit_log (actor, action, object_type, object_id)"
                    " VALUES (:actor, 'role_insert', 'test', :object_id)"
                ),
                {"actor": marker, "object_id": str(uuid.uuid4())},
            )
            connection.commit()
            found = connection.execute(
                text("SELECT count(*) FROM audit_log WHERE actor = :actor"),
                {"actor": marker},
            ).scalar()
        finally:
            connection.rollback()
            connection.execute(text("RESET ROLE"))
            connection.commit()

    assert found == 1


# --------------------------------------------------------------------------- #
# the endpoint, over a full review flow
# --------------------------------------------------------------------------- #


@dataclass
class Recorder:
    validation: list = field(default_factory=list)


def stub_services(recorder: Recorder) -> WorkflowServices:
    def index(session, audit_id, snapshot):
        return SimpleNamespace(included=1, excluded=0)

    def scanners(audit_id, snapshot, session_factory=None):
        return SimpleNamespace(failed=())

    def agents(session, audit_id):
        return SimpleNamespace(errors=(), accepted=0, calls=0)

    def validate(session, audit_id, semantic=True):
        recorder.validation.append(semantic)
        return SimpleNamespace(checked=0, verified=0, rejected=0, unresolved=0)

    return WorkflowServices(
        session_factory=get_session_factory(),
        index=index,
        scanners=scanners,
        agents=agents,
        validation=validate,
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(reviews_api, "workflow_services", lambda: stub_services(Recorder()))
    with TestClient(app) as test_client:
        yield test_client


def test_the_endpoint_returns_the_whole_flow(client, audit):
    with postgres_checkpointer() as saver:
        start_or_resume(audit, services=stub_services(Recorder()), checkpointer=saver)
    assert pending_nodes(audit) == ["review"]

    first = client.get(f"/api/v1/audits/{audit}/audit-log")
    assert first.status_code == 200
    actions = [entry["action"] for entry in first.json()["entries"]]
    assert "stage_changed" in actions
    assert "review_requested" in actions

    session = get_session_factory()()
    review_id = session.execute(
        text("SELECT id FROM reviews WHERE audit_id = :audit"), {"audit": str(audit)}
    ).scalar()
    session.close()

    decision = client.post(
        f"/api/v1/reviews/{review_id}/decision", json={"decision": "approve"}
    )
    assert decision.status_code == 200

    response = client.get(f"/api/v1/audits/{audit}/audit-log")
    body = response.json()
    actions = [entry["action"] for entry in body["entries"]]

    assert "audit_completed" in actions
    assert "review_approved" in actions
    assert body["total"] == len(body["entries"])

    review_entry = next(e for e in body["entries"] if e["action"] == "review_approved")
    assert review_entry["object_type"] == "review"
    assert review_entry["before"]["status"] == "pending"
    assert review_entry["after"]["status"] == "approved"
    assert review_entry["actor"] != "system"

    stage_entries = [e for e in body["entries"] if e["action"] == "stage_changed"]
    assert stage_entries[0]["before"]["stage"] is None
    assert stage_entries[0]["after"]["stage"] == "intake"
    # Ordered oldest first, so a reader can follow the run.
    stamps = [entry["created_at"] for entry in body["entries"]]
    assert stamps == sorted(stamps)


def test_an_unknown_audit_has_no_trail(client):
    response = client.get(f"/api/v1/audits/{uuid.uuid4()}/audit-log")

    assert response.status_code == 404
