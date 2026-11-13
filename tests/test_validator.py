"""Step 2.10: the validation pass, against a real indexed snapshot.

The point of the pass is that a finding is checked against code the audit actually
holds, so these tests use a small repository on disk rather than mocks: a finding
that cites line 40 of a nine-line file must be rejected because line 40 is not
there, not because a stub said so.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

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
from app.db.models import Audit, Evidence, Finding, FindingEvent, Repository
from app.db.session import get_session_factory
from app.intake.filtering import index_snapshot
from app.validation.validator import validate_audit

ORDERS = '''"""Order routes."""

from app.auth import get_current_user
from app.database import get_order


def read_order(order_id: str) -> dict:
    order = get_order(order_id)
    return order
'''.lstrip()

#: line 8 of ORDERS, which is the line a correct finding cites.
REAL_LINE = "    order = get_order(order_id)"


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("validator-repo")
    target = root / "app" / "routes" / "orders.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(ORDERS, encoding="utf-8")
    return root


@pytest.fixture
def audit(snapshot):
    """An indexed audit over the repository, removed afterwards."""
    session = get_session_factory()()
    repository = Repository(
        name=f"pytest-validator-{uuid.uuid4().hex[:8]}",
        source_type=SourceType.ZIP,
        storage_path=str(snapshot),
    )
    session.add(repository)
    session.commit()
    session.refresh(repository)

    audit = Audit(repository_id=repository.id, status=AuditStatus.QUEUED)
    session.add(audit)
    session.commit()
    session.refresh(audit)
    audit_id = audit.id

    index_snapshot(session, audit_id, snapshot)
    session.close()

    yield audit_id

    session = get_session_factory()()
    session.delete(session.get(Audit, audit_id))
    session.commit()
    session.delete(session.get(Repository, repository.id))
    session.commit()
    session.close()


def add_agent_finding(
    audit_id,
    *,
    path: str = "app/routes/orders.py",
    line_start: int = 8,
    line_end: int = 8,
    snippet: str = REAL_LINE,
    title: str = "read_order returns another user's order",
    category: Category = Category.SECURITY,
    severity: Severity = Severity.HIGH,
) -> uuid.UUID:
    """One finding as the agent stages store it: ai_suggested, with evidence."""
    session = get_session_factory()()
    finding = Finding(
        audit_id=audit_id,
        category=category,
        title=title,
        severity=severity,
        confidence=Decimal("0.80"),
        origin=FindingOrigin.AGENT,
        source_tool="security_agent",
        verification_status=VerificationStatus.AI_SUGGESTED,
        verdict=Verdict.ISSUE,
        reasoning="the route never resolves the caller before reading the order",
        fingerprint=uuid.uuid4().hex,
    )
    session.add(finding)
    session.flush()
    session.add(
        Evidence(
            finding_id=finding.id,
            file_path=path,
            line_start=line_start,
            line_end=line_end,
            snippet=snippet,
            kind=EvidenceKind.PRIMARY,
        )
    )
    session.commit()
    finding_id = finding.id
    session.close()
    return finding_id


def status_of(finding_id) -> VerificationStatus:
    session = get_session_factory()()
    try:
        return session.get(Finding, finding_id).verification_status
    finally:
        session.close()


def events_of(finding_id) -> list[FindingEvent]:
    session = get_session_factory()()
    try:
        return list(
            session.execute(
                select(FindingEvent)
                .where(FindingEvent.finding_id == finding_id)
                .order_by(FindingEvent.created_at)
            ).scalars()
        )
    finally:
        session.close()


def validate(audit_id, **kwargs):
    session = get_session_factory()()
    try:
        return validate_audit(session, audit_id, **kwargs)
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# a fake finding is rejected, a good one verified
# --------------------------------------------------------------------------- #


def test_a_finding_with_wrong_line_numbers_is_rejected(audit):
    """Line 40 of a nine-line file cannot hold the code the finding quotes."""
    finding_id = add_agent_finding(audit, line_start=40, line_end=42)

    outcome = validate(audit)

    assert outcome.rejected == 1
    assert status_of(finding_id) is VerificationStatus.REJECTED_FALSE_POSITIVE

    [event] = events_of(finding_id)
    assert event.from_status is VerificationStatus.AI_SUGGESTED
    assert event.to_status is VerificationStatus.REJECTED_FALSE_POSITIVE
    assert event.actor == "validator"
    assert "outside" in event.note and "9 lines" in event.note


def test_a_good_finding_is_verified(audit):
    finding_id = add_agent_finding(audit, line_start=8, line_end=8)

    outcome = validate(audit)

    assert outcome.verified == 1
    assert status_of(finding_id) is VerificationStatus.VERIFIED
    [event] = events_of(finding_id)
    assert event.to_status is VerificationStatus.VERIFIED
    assert event.note == "the deterministic checks passed"


def test_the_snippet_must_appear_at_the_cited_lines(audit):
    """The lines exist, but they hold imports, not the quoted statement."""
    finding_id = add_agent_finding(audit, line_start=3, line_end=4)

    outcome = validate(audit)

    assert outcome.rejected == 1
    assert status_of(finding_id) is VerificationStatus.REJECTED_FALSE_POSITIVE
    assert "does not appear at" in events_of(finding_id)[0].note


def test_a_cited_file_the_audit_does_not_hold_is_rejected(audit):
    finding_id = add_agent_finding(audit, path="app/routes/missing.py", line_start=1, line_end=1)

    outcome = validate(audit)

    assert outcome.rejected == 1
    assert "not in this repository" in events_of(finding_id)[0].note


# --------------------------------------------------------------------------- #
# events and the optional model check
# --------------------------------------------------------------------------- #


def test_a_second_pass_writes_no_second_event(audit):
    """A decision is written once: re-running the validator is not a status change."""
    finding_id = add_agent_finding(audit)

    validate(audit)
    second = validate(audit)

    assert second.checked == 0
    assert len(events_of(finding_id)) == 1


def test_the_optional_model_check_can_reject_a_claim_the_code_does_not_support(audit):
    """The deterministic checks pass; the model says the code is not the problem."""

    class Refusing:
        calls = 1
        cost_usd = Decimal("0.0002")

        def complete(self, agent, messages, **kwargs):
            return SimpleNamespace(text='{"supports": false, "reason": "the route is authenticated"}')

    finding_id = add_agent_finding(audit)

    outcome = validate(audit, client=Refusing(), semantic=True)

    assert outcome.rejected == 1
    assert status_of(finding_id) is VerificationStatus.REJECTED_FALSE_POSITIVE
    assert "authenticated" in events_of(finding_id)[0].note


def test_a_true_claim_is_verified_even_when_the_reason_reads_like_a_complaint(audit):
    """The verifier once answered "false" with "there are no auth checks" as its reason."""

    class Affirming:
        calls = 1
        cost_usd = Decimal("0.0002")

        def complete(self, agent, messages, **kwargs):
            return SimpleNamespace(
                text='{"claim_true": true, "reason": "There are no authentication or authorization checks here"}'
            )

    finding_id = add_agent_finding(audit)

    outcome = validate(audit, client=Affirming(), semantic=True)

    assert outcome.verified == 1 and outcome.calls == 1
    assert status_of(finding_id) is VerificationStatus.VERIFIED


def test_an_unreadable_model_answer_leaves_the_finding_unresolved(audit):
    class Mute:
        calls = 1
        cost_usd = Decimal("0.0001")

        def complete(self, agent, messages, **kwargs):
            return SimpleNamespace(text="I could not tell.")

    finding_id = add_agent_finding(audit)

    outcome = validate(audit, client=Mute(), semantic=True)

    assert outcome.unresolved == 1
    assert status_of(finding_id) is VerificationStatus.UNRESOLVED


def test_a_fenced_yes_is_read_like_the_crews_read_it(audit):
    class Fenced:
        calls = 1
        cost_usd = Decimal("0.0001")

        def complete(self, agent, messages, **kwargs):
            return SimpleNamespace(
                text='Here you go:\n```json\n{"supports": true, "reason": "the caller is never resolved"}\n```'
            )

    finding_id = add_agent_finding(audit)

    validate(audit, client=Fenced(), semantic=True)

    assert status_of(finding_id) is VerificationStatus.VERIFIED
