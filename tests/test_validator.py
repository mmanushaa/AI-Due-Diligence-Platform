"""Step 2.10b: the validation pass and its strict claim contract.

The point of the pass is that a finding is checked against code the audit actually
holds, so these tests use a small repository on disk rather than mocks: a finding
that cites line 40 of a nine-line file must be rejected because line 40 is not
there, not because a stub said so.

Every model reply here is synthetic. The code the replies are about is a two-file
fixture written by the tests: one route with no caller check, one that calls one.
"""

from __future__ import annotations

import json
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
from app.db.models import (
    Audit,
    Evidence,
    Finding,
    FindingEvent,
    Repository,
    ValidationCheck,
)
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

#: A route that does visibly check the caller before reading the order.
SECURED = '''"""Secured order routes."""

from app.auth import require_owner
from app.database import get_order


def read_order(order_id: str, user) -> dict:
    require_owner(user, order_id)
    return get_order(order_id)
'''.lstrip()

#: line 8 of ORDERS, which is the line a correct finding cites.
REAL_LINE = "    order = get_order(order_id)"
#: line 8 of SECURED, the check a contradicted "no check" claim would have missed.
CHECK_LINE = "    require_owner(user, order_id)"


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("validator-repo")
    orders = root / "app" / "routes" / "orders.py"
    orders.parent.mkdir(parents=True, exist_ok=True)
    orders.write_text(ORDERS, encoding="utf-8")
    secured = root / "app" / "routes" / "secured.py"
    secured.write_text(SECURED, encoding="utf-8")
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
    reasoning: str = "the route never resolves the caller before reading the order",
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
        reasoning=reasoning,
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


def add_scanner_finding(audit_id, *, path: str = "app/routes/orders.py") -> uuid.UUID:
    """A finding a scanner confirmed; it is not a claim for a model to judge."""
    session = get_session_factory()()
    finding = Finding(
        audit_id=audit_id,
        category=Category.SECURITY,
        title="hardcoded credential",
        severity=Severity.HIGH,
        confidence=Decimal("0.95"),
        origin=FindingOrigin.SCANNER,
        source_tool="gitleaks",
        verification_status=VerificationStatus.VERIFIED,
        verdict=Verdict.ISSUE,
        fingerprint=uuid.uuid4().hex,
    )
    session.add(finding)
    session.flush()
    session.add(
        Evidence(
            finding_id=finding.id,
            file_path=path,
            line_start=8,
            line_end=8,
            snippet=REAL_LINE,
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


def checks_of(finding_id) -> list[ValidationCheck]:
    session = get_session_factory()()
    try:
        return list(
            session.execute(
                select(ValidationCheck)
                .where(ValidationCheck.finding_id == finding_id)
                .order_by(ValidationCheck.created_at)
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


class Reply:
    """A synthetic model reply with the cost and time the client would report."""

    def __init__(self, text: str, *, cost: str = "0.0002", latency_ms: int = 640) -> None:
        self.text = text
        self.cost_usd = Decimal(cost)
        self.latency_ms = latency_ms

    def complete(self, agent, messages, **kwargs):
        return SimpleNamespace(
            text=self.text, cost_usd=self.cost_usd, latency_ms=self.latency_ms
        )


def json_reply(verdict: str, quote: str, **extra) -> Reply:
    return Reply(json.dumps({"verdict": verdict, "quote": quote, **extra}))


def never_called(agent, messages, **kwargs):  # pragma: no cover - asserted against
    raise AssertionError("this finding must not be sent to the model")


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
# the strict verdict/quote contract
# --------------------------------------------------------------------------- #


def test_a_negative_claim_is_supported_when_the_code_has_no_check(audit):
    """The polarity bug: "there is no check" is supported by code without one."""
    finding_id = add_agent_finding(
        audit,
        title="read_order has no caller check",
        reasoning="there is no authorization call anywhere in this function",
    )

    outcome = validate(audit, client=json_reply("supported", REAL_LINE), semantic=True)

    assert outcome.verified == 1
    assert outcome.rejected == 0
    assert status_of(finding_id) is VerificationStatus.VERIFIED
    [check] = checks_of(finding_id)
    assert check.verdict == "supported"
    assert check.quote == REAL_LINE.strip()


def test_a_claim_is_contradicted_when_the_code_visibly_calls_a_check(audit):
    finding_id = add_agent_finding(
        audit,
        path="app/routes/secured.py",
        line_start=8,
        line_end=8,
        snippet=CHECK_LINE,
        title="read_order has no caller check",
    )

    outcome = validate(
        audit, client=json_reply("contradicted", CHECK_LINE), semantic=True
    )

    assert outcome.rejected == 1
    assert status_of(finding_id) is VerificationStatus.REJECTED_FALSE_POSITIVE
    [check] = checks_of(finding_id)
    assert check.verdict == "contradicted"
    assert check.quote == CHECK_LINE.strip()
    assert "contradicted" in events_of(finding_id)[0].note


def test_a_negative_claim_is_not_inverted_into_a_rejection(audit):
    """A supported answer about missing code must never become a false positive."""
    finding_id = add_agent_finding(
        audit,
        title="the export route does not resolve the caller",
        reasoning="no ownership or membership check appears in the cited function",
    )

    validate(audit, client=json_reply("supported", REAL_LINE), semantic=True)

    assert status_of(finding_id) is VerificationStatus.VERIFIED


def test_a_quote_that_is_not_in_the_cited_code_leaves_the_finding_unresolved(audit):
    finding_id = add_agent_finding(audit)

    outcome = validate(
        audit,
        client=json_reply("supported", "the route checks the caller's role"),
        semantic=True,
    )

    assert outcome.unresolved == 1
    assert outcome.verified == 0
    assert status_of(finding_id) is VerificationStatus.UNRESOLVED
    [check] = checks_of(finding_id)
    assert check.verdict == "supported"
    assert check.quote == "the route checks the caller's role"
    assert "does not appear" in events_of(finding_id)[0].note


def test_invalid_json_leaves_the_finding_unresolved(audit):
    finding_id = add_agent_finding(audit)
    reply = Reply("I could not tell from this code.")

    outcome = validate(audit, client=reply, semantic=True)

    assert outcome.unresolved == 1
    assert status_of(finding_id) is VerificationStatus.UNRESOLVED
    [check] = checks_of(finding_id)
    assert check.verdict == "cannot_tell"
    assert check.quote is None
    assert check.raw_reply == "I could not tell from this code."


def test_a_reply_with_a_verdict_outside_the_contract_is_unresolved(audit):
    finding_id = add_agent_finding(audit)
    reply = Reply(json.dumps({"verdict": "yes", "quote": REAL_LINE}))

    validate(audit, client=reply, semantic=True)

    assert status_of(finding_id) is VerificationStatus.UNRESOLVED


def test_cannot_tell_leaves_the_finding_unresolved(audit):
    finding_id = add_agent_finding(audit)

    outcome = validate(
        audit, client=json_reply("cannot_tell", REAL_LINE), semantic=True
    )

    assert outcome.unresolved == 1
    assert status_of(finding_id) is VerificationStatus.UNRESOLVED
    assert checks_of(finding_id)[0].verdict == "cannot_tell"


def test_the_check_records_the_range_sent_and_the_masked_reply(audit):
    finding_id = add_agent_finding(audit)
    token = "sk-" + "a" * 24
    reply = Reply(
        json.dumps({"verdict": "supported", "quote": REAL_LINE, "note": token}),
        cost="0.0003",
        latency_ms=812,
    )

    validate(audit, client=reply, semantic=True)

    [check] = checks_of(finding_id)
    assert check.audit_id == audit
    assert check.eval_run_id is None
    assert check.cited_file == "app/routes/orders.py"
    assert (check.line_start, check.line_end) == (8, 8)
    assert check.latency_ms == 812
    assert check.cost_usd == Decimal("0.0003")
    assert check.raw_reply is not None
    assert token not in check.raw_reply
    assert "REDACTED" in check.raw_reply


def test_a_scanner_finding_is_never_sent_to_the_model(audit):
    finding_id = add_scanner_finding(audit)
    client = SimpleNamespace(complete=never_called)

    outcome = validate(audit, client=client, semantic=True)

    assert outcome.calls == 0
    assert status_of(finding_id) is VerificationStatus.VERIFIED
    assert checks_of(finding_id) == []


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
    """The deterministic checks pass; the model finds the code shows a check."""
    finding_id = add_agent_finding(
        audit,
        path="app/routes/secured.py",
        line_start=8,
        line_end=8,
        snippet=CHECK_LINE,
    )

    outcome = validate(
        audit, client=json_reply("contradicted", CHECK_LINE), semantic=True
    )

    assert outcome.rejected == 1
    assert status_of(finding_id) is VerificationStatus.REJECTED_FALSE_POSITIVE
    assert "contradicted" in events_of(finding_id)[0].note


def test_a_true_claim_is_verified_even_when_the_reason_reads_like_a_complaint(audit):
    """The verifier once answered false with "there are no auth checks" as its reason."""

    class Affirming(Reply):
        def __init__(self) -> None:
            super().__init__(
                json.dumps(
                    {
                        "verdict": "supported",
                        "quote": REAL_LINE,
                    }
                )
            )

    finding_id = add_agent_finding(audit)

    outcome = validate(audit, client=Affirming(), semantic=True)

    assert outcome.verified == 1 and outcome.calls == 1
    assert status_of(finding_id) is VerificationStatus.VERIFIED


def test_an_unreadable_model_answer_leaves_the_finding_unresolved(audit):
    finding_id = add_agent_finding(audit)

    outcome = validate(audit, client=Reply("I could not tell."), semantic=True)

    assert outcome.unresolved == 1
    assert status_of(finding_id) is VerificationStatus.UNRESOLVED


def test_a_fenced_reply_is_read_like_the_crews_read_it(audit):
    class Fenced(Reply):
        def __init__(self) -> None:
            super().__init__(
                'Here you go:\n```json\n'
                + json.dumps({"verdict": "supported", "quote": REAL_LINE})
                + "\n```"
            )

    finding_id = add_agent_finding(audit)

    validate(audit, client=Fenced(), semantic=True)

    assert status_of(finding_id) is VerificationStatus.VERIFIED
