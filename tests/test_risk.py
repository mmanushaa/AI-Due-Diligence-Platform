"""Step 3.3: risk scoring and aggregation.

Two things are being protected here. The arithmetic must be exact and readable - fixed
inputs, fixed expected scores, each finding's factors visible - and the level must never
be an average: one verified critical among a hundred lows is still a critical audit.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.enums import (
    AuditStatus,
    Category,
    FindingOrigin,
    Severity,
    SourceType,
    VerificationStatus,
    Verdict,
)
from app.db.models import Audit, Finding, Repository, RiskAssessment, RiskFactor
from app.db.session import get_session_factory
from app.risk.assessment import assess, current_assessment, score_findings
from app.risk.scoring import FindingInput, evaluate, score_finding


def f(
    severity: Severity,
    confidence: str | None,
    status: VerificationStatus,
    *,
    category: Category | None = None,
    title: str = "",
    paths: tuple[str, ...] = (),
) -> FindingInput:
    return FindingInput(
        severity=severity,
        confidence=Decimal(confidence) if confidence is not None else None,
        status=status,
        category=category,
        title=title,
        evidence_paths=paths,
    )


def test_each_factor_and_the_total_are_exact():
    findings = [
        f(Severity.CRITICAL, "0.9", VerificationStatus.VERIFIED),
        f(Severity.HIGH, "1.0", VerificationStatus.SCANNER_CONFIRMED),
        f(Severity.MEDIUM, "0.5", VerificationStatus.AI_SUGGESTED),
        f(Severity.LOW, "1.0", VerificationStatus.UNRESOLVED),
        f(Severity.INFO, "0.25", VerificationStatus.UNRESOLVED),
    ]

    score = evaluate(findings)

    # critical 10 × 0.9 × 1.0, high 7 × 1.0 × 1.0, medium 4 × 0.5 × 0.8,
    # low 1.5 × 1.0 × 0.6, info 0.5 × 0.25 × 0.6
    assert [str(item.score) for item in score.per_finding] == [
        "9.0000",
        "7.0000",
        "1.6000",
        "0.9000",
        "0.0750",
    ]
    assert [str(item.severity_weight) for item in score.per_finding] == [
        "10", "7", "4", "1.5", "0.5",
    ]
    assert [str(item.evidence_confidence) for item in score.per_finding] == [
        "0.9000", "1.0000", "0.5000", "1.0000", "0.2500",
    ]
    assert [str(item.exposure_multiplier) for item in score.per_finding] == [
        "1.0000", "1.0000", "0.8000", "0.6000", "0.6000",
    ]
    assert str(score.score) == "18.5750"
    assert score.level is Severity.CRITICAL
    assert score.floor is Severity.CRITICAL
    assert score.rule_applied == "severity_floor"
    assert score.rules == ()


def test_one_verified_critical_among_a_hundred_lows_stays_critical():
    findings = [f(Severity.LOW, "1.0", VerificationStatus.VERIFIED) for _ in range(100)]
    findings.append(f(Severity.CRITICAL, "0.95", VerificationStatus.VERIFIED))

    score = evaluate(findings)

    assert score.counts["low"] == 100
    assert score.counts["critical"] == 1
    assert score.floor is Severity.CRITICAL
    assert score.level is Severity.CRITICAL
    # The aggregate is a sum, so the hundred lows raised the score, not diluted it.
    assert score.score == Decimal("159.5000")


def test_adding_low_findings_never_lowers_the_level_or_the_score():
    base = [f(Severity.HIGH, "0.9", VerificationStatus.VERIFIED)]
    noisy = base + [f(Severity.LOW, "1.0", VerificationStatus.VERIFIED) for _ in range(50)]

    quiet = evaluate(base)
    loud = evaluate(noisy)

    assert quiet.level is Severity.HIGH
    assert loud.level is Severity.HIGH
    assert loud.score > quiet.score


def test_three_highs_escalate_to_critical_even_without_a_critical_finding():
    findings = [f(Severity.HIGH, "0.5", VerificationStatus.VERIFIED) for _ in range(3)]

    score = evaluate(findings)

    assert score.floor is Severity.HIGH
    assert score.level is Severity.CRITICAL
    assert score.rules == ("many_high_findings",)
    assert score.rule_applied == "severity_floor+many_high_findings"
    assert "many_high_findings" in score.rationale


def test_a_large_aggregate_escalates_one_level_above_the_floor():
    findings = [f(Severity.MEDIUM, "0.9", VerificationStatus.VERIFIED) for _ in range(8)]

    score = evaluate(findings)

    assert str(score.score) == "28.8000"  # 8 × 4 × 0.9
    assert score.floor is Severity.MEDIUM
    assert score.level is Severity.HIGH
    assert score.rules == ("aggregate_score",)
    assert score.rule_applied == "severity_floor+aggregate_score"


def test_escalation_cannot_lower_a_critical_floor():
    findings = [f(Severity.CRITICAL, "0.2", VerificationStatus.VERIFIED)]
    findings += [f(Severity.HIGH, "0.2", VerificationStatus.VERIFIED) for _ in range(3)]

    score = evaluate(findings)

    assert score.floor is Severity.CRITICAL
    assert score.level is Severity.CRITICAL


def test_a_rejected_claim_is_neither_floor_nor_score():
    rejected = f(Severity.CRITICAL, "1.0", VerificationStatus.REJECTED_FALSE_POSITIVE)
    kept = f(Severity.LOW, "1.0", VerificationStatus.VERIFIED)

    score = evaluate([rejected, kept])

    assert score.counts["critical"] == 0
    assert score.counts["low"] == 1
    assert score.level is Severity.LOW
    assert score.score == Decimal("1.5000")
    assert score.rationale.startswith("1 finding(s) counted, 1 rejected")


def test_an_unverified_claim_contributes_less_than_a_verified_one():
    verified = score_finding(f(Severity.HIGH, "1.0", VerificationStatus.VERIFIED))
    unresolved = score_finding(f(Severity.HIGH, "1.0", VerificationStatus.UNRESOLVED))

    assert str(verified.score) == "7.0000"
    assert str(unresolved.score) == "4.2000"
    assert unresolved.exposure_multiplier == Decimal("0.6000")


def test_a_missing_confidence_uses_the_documented_default():
    item = score_finding(f(Severity.MEDIUM, None, VerificationStatus.VERIFIED))

    assert str(item.evidence_confidence) == "0.5000"
    assert str(item.score) == "2.0000"


def test_a_committed_secret_is_scored_as_more_exposed():
    item = score_finding(
        f(Severity.HIGH, "1.0", VerificationStatus.VERIFIED, category=Category.SECRET)
    )

    assert item.hints == ("committed_secret",)
    assert str(item.exposure_multiplier) == "1.2500"
    assert str(item.score) == "8.7500"
    assert "committed_secret" in item.notes


def test_evidence_that_only_lives_in_tests_is_less_exposed():
    only_tests = score_finding(
        f(Severity.HIGH, "1.0", VerificationStatus.VERIFIED, paths=("tests/test_api.py",))
    )
    mixed = score_finding(
        f(
            Severity.HIGH,
            "1.0",
            VerificationStatus.VERIFIED,
            paths=("tests/test_api.py", "app/api.py"),
        )
    )

    assert only_tests.hints == ("test_only_evidence",)
    assert str(only_tests.exposure_multiplier) == "0.5000"
    assert str(only_tests.score) == "3.5000"
    # One production path is enough: the finding is not test-only.
    assert mixed.hints == ()
    assert str(mixed.score) == "7.0000"


def test_a_confirmed_credential_escalates_to_critical_by_itself():
    findings = [
        f(Severity.LOW, "1.0", VerificationStatus.VERIFIED, category=Category.SECRET,
          title="AWS key committed in settings"),
    ]

    score = evaluate(findings)

    assert score.floor is Severity.LOW
    assert score.level is Severity.CRITICAL
    assert score.rules == ("confirmed_credential",)
    assert score.rule_applied == "severity_floor+confirmed_credential"
    assert "confirmed_credential" in score.rationale


def test_an_unconfirmed_credential_does_not_escalate():
    score = evaluate(
        [f(Severity.LOW, "1.0", VerificationStatus.AI_SUGGESTED, category=Category.SECRET)]
    )

    assert score.level is Severity.LOW
    assert score.rules == ()


def test_a_copyleft_license_escalates_one_level_not_to_critical():
    score = evaluate(
        [
            f(
                Severity.MEDIUM,
                "1.0",
                VerificationStatus.SCANNER_CONFIRMED,
                category=Category.LICENSE,
                title="GPL-2.0-only dependency in the distributed bundle",
            )
        ]
    )

    assert score.floor is Severity.MEDIUM
    assert score.level is Severity.HIGH
    assert score.rules == ("copyleft_license",)
    assert score.rule_applied == "severity_floor+copyleft_license"


def test_aggregate_volume_alone_never_reaches_critical():
    findings = [
        f(Severity.LOW, "1.0", VerificationStatus.VERIFIED, paths=("app/main.py",))
        for _ in range(30)
    ]

    score = evaluate(findings)

    assert score.score == Decimal("45.0000")  # 30 × 1.5, well past the threshold
    assert score.floor is Severity.LOW
    assert score.level is Severity.MEDIUM
    assert score.rules == ("aggregate_score",)


# --- the database half ------------------------------------------------------


@pytest.fixture
def audit(tmp_path):
    session = get_session_factory()()
    repository = Repository(
        name=f"pytest-risk-{uuid.uuid4().hex[:8]}",
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
    session.delete(session.get(Audit, audit_id))
    session.commit()
    session.delete(session.get(Repository, repository.id))
    session.commit()
    session.close()


def add_finding(
    audit_id, severity, confidence, status, category: Category = Category.SECURITY
) -> uuid.UUID:
    session = get_session_factory()()
    finding = Finding(
        audit_id=audit_id,
        category=category,
        title=f"{severity.value} finding",
        severity=severity,
        confidence=confidence,
        origin=FindingOrigin.SCANNER,
        source_tool="semgrep",
        verification_status=status,
        verdict=Verdict.ISSUE,
        fingerprint=uuid.uuid4().hex,
    )
    session.add(finding)
    session.commit()
    session.refresh(finding)
    finding_id = finding.id
    session.close()
    return finding_id


def test_assess_stores_the_level_score_rule_and_factors(audit):
    add_finding(audit, Severity.HIGH, Decimal("1.0"), VerificationStatus.VERIFIED)
    add_finding(audit, Severity.MEDIUM, Decimal("0.5"), VerificationStatus.AI_SUGGESTED)
    add_finding(audit, Severity.LOW, Decimal("1.0"), VerificationStatus.REJECTED_FALSE_POSITIVE)

    session = get_session_factory()()
    try:
        assessment = assess(session, audit)
    finally:
        session.close()

    assert assessment.overall_level is Severity.HIGH
    assert assessment.overall_score == Decimal("8.6000")
    assert assessment.rule_applied == "severity_floor"
    assert assessment.status.value == "final"
    summary = assessment.technical_summary
    assert summary["counts"] == {"critical": 0, "high": 1, "medium": 1, "low": 0, "info": 0}
    assert summary["rejected"] == 1
    assert summary["highest"] == "high"
    assert summary["score"] == "8.6000"
    assert len(summary["factors"]) == 2

    session = get_session_factory()()
    try:
        factors = session.execute(
            select(RiskFactor)
            .where(RiskFactor.assessment_id == assessment.id)
            .order_by(RiskFactor.finding_score.desc())
        ).scalars().all()
    finally:
        session.close()

    assert [str(row.finding_score) for row in factors] == ["7.0000", "1.6000"]
    assert [str(row.severity_weight) for row in factors] == ["7.0000", "4.0000"]
    assert factors[0].notes == "7 weight × 1.0000 confidence × 1.0000 exposure = 7.0000"
    assert factors[1].notes == "4 weight × 0.5000 confidence × 0.8000 exposure = 1.6000"
    assert summary["factors"][0]["finding_id"] == str(factors[0].finding_id)


def test_a_critical_audit_goes_to_review_and_rescoring_supersedes(audit):
    add_finding(audit, Severity.CRITICAL, Decimal("0.9"), VerificationStatus.VERIFIED)

    session = get_session_factory()()
    try:
        first = assess(session, audit)
    finally:
        session.close()
    assert first.status.value == "pending_review"
    assert first.supersedes_id is None

    session = get_session_factory()()
    try:
        second = assess(session, audit)
        latest = current_assessment(session, audit)
    finally:
        session.close()

    assert second.supersedes_id == first.id
    assert latest.id == second.id
    assert latest.overall_level is Severity.CRITICAL


def test_assess_stores_the_exposure_hints_it_used(audit):
    secret = add_finding(
        audit, Severity.HIGH, Decimal("1.0"), VerificationStatus.VERIFIED,
        category=Category.SECRET,
    )

    session = get_session_factory()()
    try:
        assessment = assess(session, audit)
    finally:
        session.close()

    assert assessment.overall_level is Severity.CRITICAL
    assert assessment.rule_applied == "severity_floor+confirmed_credential"
    summary_factor = assessment.technical_summary["factors"][0]
    assert summary_factor["finding_id"] == str(secret)
    assert summary_factor["hints"] == ["committed_secret"]
    assert summary_factor["exposure_multiplier"] == "1.2500"

    session = get_session_factory()()
    try:
        stored = session.execute(
            select(RiskFactor).where(RiskFactor.assessment_id == assessment.id)
        ).scalars().one()
    finally:
        session.close()
    assert stored.notes.endswith("[committed_secret]")
    assert str(stored.exposure_multiplier) == "1.2500"


def test_score_findings_reads_rows_without_writing(audit):
    add_finding(audit, Severity.HIGH, Decimal("0.5"), VerificationStatus.VERIFIED)

    session = get_session_factory()()
    try:
        findings = session.execute(
            select(Finding).where(Finding.audit_id == audit)
        ).scalars().all()
        score = score_findings(list(findings))
        stored = session.execute(
            select(RiskAssessment).where(RiskAssessment.audit_id == audit)
        ).scalars().all()
    finally:
        session.close()

    assert str(score.score) == "3.5000"
    assert stored == []
