"""Step 3.4: the human review gate.

A critical audit must stop, and stay stopped across a restart, until a reviewer
decides. These tests drive the real workflow with the real Postgres checkpointer and
stub only the stages, then call the review endpoints over HTTP with the stub services
in place, so the pause and the resume are the production paths.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.api import reviews as reviews_api
from app.db.enums import (
    AssessmentStatus,
    AuditStatus,
    Category,
    EvidenceKind,
    FindingOrigin,
    ReviewStatus,
    Severity,
    SourceType,
    VerificationStatus,
    Verdict,
)
from app.db.models import (
    Audit,
    AuditLog,
    Evidence,
    Finding,
    Repository,
    Review,
    ReviewDecisionRow,
    RiskAssessment,
)
from app.db.session import get_session_factory
from app.main import app
from app.workflow.graph import pending_nodes, postgres_checkpointer, start_or_resume
from app.workflow.nodes import WorkflowServices

REVIEWS_URL = "/api/v1/reviews"


@dataclass
class Recorder:
    index: list = field(default_factory=list)
    scanners: list = field(default_factory=list)
    agents: list = field(default_factory=list)
    validation: list = field(default_factory=list)


def stub_services(recorder: Recorder) -> WorkflowServices:
    """Every stage stubbed except the risk assessment, which is under test."""

    def index(session, audit_id, snapshot):
        recorder.index.append(str(audit_id))
        return SimpleNamespace(included=1, excluded=0)

    def scanners(audit_id, snapshot, session_factory=None):
        recorder.scanners.append(str(audit_id))
        return SimpleNamespace(failed=())

    def agents(session, audit_id):
        recorder.agents.append(str(audit_id))
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
def audit(tmp_path):
    """An indexed-shaped audit over a small repository, with one critical finding."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("print('hello')\n", encoding="utf-8")

    session = get_session_factory()()
    repository = Repository(
        name=f"pytest-review-{uuid.uuid4().hex[:8]}",
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

    session = get_session_factory()()
    # Since step 3.5 review_decisions is append-only, and deleting the audit
    # cascades into it; step around the trigger here and here only.
    session.execute(text("ALTER TABLE review_decisions DISABLE TRIGGER USER"))
    session.delete(session.get(Audit, audit_id))
    session.commit()
    session.execute(text("ALTER TABLE review_decisions ENABLE TRIGGER USER"))
    session.delete(session.get(Repository, repository.id))
    session.commit()
    session.close()


def audit_row(audit_id) -> Audit:
    session = get_session_factory()()
    try:
        return session.get(Audit, audit_id)
    finally:
        session.close()


def reviews_of(audit_id) -> list[Review]:
    session = get_session_factory()()
    try:
        return list(
            session.execute(
                select(Review).where(Review.audit_id == audit_id).order_by(Review.created_at)
            ).scalars()
        )
    finally:
        session.close()


def assessments_of(audit_id) -> list[RiskAssessment]:
    session = get_session_factory()()
    try:
        return list(
            session.execute(
                select(RiskAssessment)
                .where(RiskAssessment.audit_id == audit_id)
                .order_by(RiskAssessment.created_at)
            ).scalars()
        )
    finally:
        session.close()


def decisions_of(review_id) -> list[ReviewDecisionRow]:
    session = get_session_factory()()
    try:
        return list(
            session.execute(
                select(ReviewDecisionRow).where(ReviewDecisionRow.review_id == review_id)
            ).scalars()
        )
    finally:
        session.close()


def run_to_pause(audit_id, recorder: Recorder) -> list[str]:
    """Run the workflow with the stubs; it should stop in the review node."""
    with postgres_checkpointer() as saver:
        start_or_resume(audit_id, services=stub_services(recorder), checkpointer=saver)
    return pending_nodes(audit_id)


def decide(client: TestClient, review_id, **payload):
    return client.post(f"{REVIEWS_URL}/{review_id}/decision", json=payload)


@pytest.fixture
def client(monkeypatch, audit):
    """A TestClient whose resumed workflow uses the stubs, not docker or a model."""
    recorder = Recorder()
    monkeypatch.setattr(reviews_api, "workflow_services", lambda: stub_services(recorder))
    with TestClient(app) as test_client:
        yield test_client, recorder


# --------------------------------------------------------------------------- #
# the gate: a critical audit stops and stays stopped
# --------------------------------------------------------------------------- #


def test_a_critical_audit_pauses_at_awaiting_review(audit):
    recorder = Recorder()

    pending = run_to_pause(audit, recorder)

    assert pending == ["review"]
    row = audit_row(audit)
    assert row.status is AuditStatus.AWAITING_REVIEW
    assert row.finished_at is None
    reviews = reviews_of(audit)
    assert len(reviews) == 1
    assert reviews[0].status is ReviewStatus.PENDING
    assert reviews[0].requested_at is not None
    assessment = assessments_of(audit)
    assert assessment[-1].overall_level is Severity.CRITICAL
    assert reviews[0].assessment_id == assessment[-1].id


def test_the_pause_survives_a_restart(audit):
    """A new worker (a new process, a new checkpointer) still finds the run waiting."""
    recorder = Recorder()
    assert run_to_pause(audit, recorder) == ["review"]

    # The worker restarts and the message is delivered again.
    with postgres_checkpointer() as saver:
        start_or_resume(audit, services=stub_services(Recorder()), checkpointer=saver)

    assert pending_nodes(audit) == ["review"]
    assert len(reviews_of(audit)) == 1  # the node did not create a second row
    assert audit_row(audit).status is AuditStatus.AWAITING_REVIEW


def test_a_non_critical_audit_does_not_pause(tmp_path):
    """Nothing below critical needs a human: the run finishes on its own."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("print('hello')\n", encoding="utf-8")
    session = get_session_factory()()
    repository = Repository(
        name=f"pytest-review-low-{uuid.uuid4().hex[:8]}",
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

    try:
        recorder = Recorder()
        with postgres_checkpointer() as saver:
            start_or_resume(audit_id, services=stub_services(recorder), checkpointer=saver)

        assert pending_nodes(audit_id) == []
        assert audit_row(audit_id).status is AuditStatus.COMPLETED
        assert reviews_of(audit_id) == []
    finally:
        session = get_session_factory()()
        session.delete(session.get(Audit, audit_id))
        session.commit()
        session.delete(session.get(Repository, repository.id))
        session.commit()
        session.close()


# --------------------------------------------------------------------------- #
# the endpoints
# --------------------------------------------------------------------------- #


def test_the_pending_list_shows_a_waiting_audit(client, audit):
    test_client, _ = client
    run_to_pause(audit, Recorder())

    response = test_client.get(REVIEWS_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 1
    entry = next(item for item in body["reviews"] if item["audit_id"] == str(audit))
    assert entry["status"] == "pending"
    assert entry["level"] == "critical"


def test_override_without_a_reason_is_rejected(client, audit):
    test_client, _ = client
    run_to_pause(audit, Recorder())
    review = reviews_of(audit)[0]

    response = decide(test_client, review.id, decision="override", new_level="medium")

    assert response.status_code == 400
    assert "reason" in response.json()["detail"]
    # Nothing was recorded and the run is still waiting.
    assert reviews_of(audit)[0].status is ReviewStatus.PENDING
    assert decisions_of(review.id) == []
    assert pending_nodes(audit) == ["review"]


def test_override_also_needs_a_new_level(client, audit):
    test_client, _ = client
    run_to_pause(audit, Recorder())
    review = reviews_of(audit)[0]

    response = decide(test_client, review.id, decision="override", reason="accepted risk")

    assert response.status_code == 400
    assert "new level" in response.json()["detail"]


def test_approve_finalizes_the_audit(client, audit):
    test_client, recorder = client
    run_to_pause(audit, Recorder())
    review = reviews_of(audit)[0]
    original = assessments_of(audit)[0]

    response = decide(test_client, review.id, decision="approve")

    assert response.status_code == 200
    assert response.json()["review_status"] == "approved"
    assert response.json()["audit_status"] == "completed"
    assert reviews_of(audit)[0].status is ReviewStatus.APPROVED
    assert audit_row(audit).status is AuditStatus.COMPLETED
    assert audit_row(audit).finished_at is not None
    assert pending_nodes(audit) == []

    decisions = decisions_of(review.id)
    assert len(decisions) == 1
    assert decisions[0].decision.value == "approve"
    assert decisions[0].original_level is Severity.CRITICAL
    assert decisions[0].final_level is Severity.CRITICAL

    # The assessment the pipeline produced is untouched.
    after = assessments_of(audit)
    assert len(after) == 1
    assert after[0].id == original.id
    assert after[0].overall_level is Severity.CRITICAL
    assert after[0].rationale == original.rationale


def test_override_supersedes_the_assessment_without_editing_it(client, audit):
    test_client, _ = client
    run_to_pause(audit, Recorder())
    review = reviews_of(audit)[0]
    original = assessments_of(audit)[0]

    response = decide(
        test_client,
        review.id,
        decision="override",
        reason="the finding is inside a feature we do not ship",
        new_level="medium",
    )

    assert response.status_code == 200
    assert response.json()["review_status"] == "overridden"
    assert response.json()["superseded_assessment_id"] == str(original.id)

    rows = assessments_of(audit)
    assert len(rows) == 2
    old, new = rows
    # The original keeps every value it had.
    assert (old.id, old.overall_level, old.status, old.rationale) == (
        original.id,
        Severity.CRITICAL,
        original.status,
        original.rationale,
    )
    assert new.id != old.id
    assert new.overall_level is Severity.MEDIUM
    assert new.supersedes_id == old.id
    assert new.status is AssessmentStatus.FINAL
    assert new.rule_applied == "human_override"

    decision = decisions_of(review.id)[0]
    assert decision.original_level is Severity.CRITICAL
    assert decision.final_level is Severity.MEDIUM
    assert decision.reason
    assert audit_row(audit).status is AuditStatus.COMPLETED


def test_request_evidence_sets_the_status_and_resumes_from_validation(client, audit):
    test_client, recorder = client
    run_to_pause(audit, Recorder())
    review = reviews_of(audit)[0]

    response = decide(
        test_client, review.id, decision="request_evidence", reason="show the caller check"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["review_status"] == "needs_evidence"
    assert body["resumed"] is True

    # The decision is recorded as the audit needing evidence ...
    decision = decisions_of(review.id)[0]
    assert decision.decision.value == "request_evidence"
    assert decision.reason == "show the caller check"
    session = get_session_factory()()
    try:
        actions = [
            log.action
            for log in session.execute(
                select(AuditLog).where(AuditLog.object_id == str(audit))
            ).scalars()
        ]
    finally:
        session.close()
    assert "audit_needs_evidence" in actions

    # ... and the workflow ran validation again, with the note in hand.
    assert recorder.validation == [True]
    assert [review.status for review in reviews_of(audit)].count(ReviewStatus.NEEDS_EVIDENCE) == 1
    # The evidence is still critical, so the run pauses for a fresh decision.
    assert pending_nodes(audit) == ["review"]
    assert audit_row(audit).status is AuditStatus.AWAITING_REVIEW


def test_a_decided_review_cannot_be_decided_again(client, audit):
    test_client, _ = client
    run_to_pause(audit, Recorder())
    review = reviews_of(audit)[0]
    decide(test_client, review.id, decision="approve")

    response = decide(test_client, review.id, decision="approve")

    assert response.status_code == 409
