"""Step 3.9: the executive report.

The two requirements that matter are behavioural, not structural: a citation of a
finding that does not exist must be caught, and a scanner that failed must appear in
limitations. Everything else here checks that the sections are actually built from the
stored rows.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db.enums import (
    AssessmentStatus,
    AuditStatus,
    Category,
    EvidenceKind,
    FindingOrigin,
    ReviewDecision,
    ReviewStatus,
    Severity,
    SourceType,
    VerificationStatus,
    Verdict,
)
from app.db.models import (
    Audit,
    Evidence,
    Finding,
    Repository,
    Review,
    ReviewDecisionRow,
    RiskAssessment,
    ScannerFailure,
)
from app.db.session import get_session_factory
from app.main import app
from app.reports import build_report, render_html
from app.reports.document import Claim
from app.reports.reporter import validate_citations, write_summary

SUITES = [
    ("SQL injection in the export route", Category.SECURITY, Severity.CRITICAL,
     VerificationStatus.VERIFIED, Verdict.ISSUE, "app/export.py", 41),
    ("No rate limit on the login endpoint", Category.SECURITY, Severity.HIGH,
     VerificationStatus.VERIFIED, Verdict.ISSUE, "app/auth.py", 12),
    ("Unpinned dependency: flask==3.0.0", Category.DEPENDENCY, Severity.MEDIUM,
     VerificationStatus.SCANNER_CONFIRMED, Verdict.ISSUE, "requirements.txt", 1),
    ("License of bundled asset is unknown", Category.LICENSE, Severity.LOW,
     VerificationStatus.UNRESOLVED, Verdict.INSUFFICIENT_EVIDENCE, "LICENSE", 1),
]


@pytest.fixture
def audit(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("print('hello')\n", encoding="utf-8")
    session = get_session_factory()()
    repository = Repository(
        name=f"pytest-report-{uuid.uuid4().hex[:8]}",
        source_type=SourceType.ZIP,
        storage_path=str(tmp_path),
    )
    session.add(repository)
    session.commit()
    session.refresh(repository)
    row = Audit(
        repository_id=repository.id,
        status=AuditStatus.COMPLETED,
        current_stage=None,
        cost_usd=Decimal("0.0140"),
        budget_usd=Decimal("1.0000"),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    audit_id = row.id

    findings = []
    for title, category, severity, status, verdict, path, line in SUITES:
        finding = Finding(
            audit_id=audit_id,
            category=category,
            title=title,
            severity=severity,
            confidence=Decimal("0.85"),
            origin=FindingOrigin.SCANNER,
            source_tool="semgrep",
            verification_status=status,
            verdict=verdict,
            recommendation=f"Fix: {title}",
            fingerprint=uuid.uuid4().hex,
        )
        session.add(finding)
        session.flush()
        session.add(
            Evidence(
                finding_id=finding.id,
                file_path=path,
                line_start=line,
                line_end=line,
                snippet="...",
                kind=EvidenceKind.PRIMARY,
            )
        )
        findings.append(finding)

    assessment = RiskAssessment(
        audit_id=audit_id,
        overall_level=Severity.CRITICAL,
        overall_score=Decimal("0.9000"),
        rule_applied="severity_floor",
        rationale="Highest non-rejected finding severity is critical.",
        status=AssessmentStatus.PENDING_REVIEW,
    )
    session.add(assessment)
    session.commit()
    session.refresh(assessment)

    review = Review(
        audit_id=audit_id,
        assessment_id=assessment.id,
        status=ReviewStatus.APPROVED,
    )
    session.add(review)
    session.commit()
    session.refresh(review)
    session.add(
        ReviewDecisionRow(
            review_id=review.id,
            reviewer_id=None,
            decision=ReviewDecision.APPROVE,
            reason="Accepted the critical assessment.",
            original_level=Severity.CRITICAL,
            final_level=Severity.CRITICAL,
        )
    )
    session.add(
        ScannerFailure(
            audit_id=audit_id,
            tool="scancode",
            reason="image exited with status 1: licence database unavailable",
        )
    )
    session.commit()
    session.close()

    yield {"audit_id": audit_id, "repository_id": repository.id, "findings": findings}

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


def test_every_summary_claim_cites_a_stored_finding(audit):
    session = get_session_factory()()
    try:
        document = build_report(session, audit["audit_id"])
    finally:
        session.close()

    known = {str(f.id) for f in audit["findings"]}
    assert document.claims
    for claim in document.claims:
        assert claim.finding_ids
        assert set(claim.finding_ids) <= known
    assert document.citations_dropped == []


def test_an_invented_finding_id_is_dropped_and_flagged(audit):
    session = get_session_factory()()
    try:
        document = build_report(session, audit["audit_id"])
    finally:
        session.close()

    real = document.top_findings[0].id
    invented = str(uuid.uuid4())
    claims = [
        Claim(text="A supported sentence.", finding_ids=(real,)),
        Claim(text="A sentence that cites a finding nobody stored.", finding_ids=(invented,)),
        Claim(text="A sentence mixing both.", finding_ids=(real, invented)),
    ]

    kept, dropped = validate_citations(claims, {real})

    assert [claim.text for claim in kept] == ["A supported sentence."]
    assert [claim.text for claim in dropped] == [
        "A sentence that cites a finding nobody stored.",
        "A sentence mixing both.",
    ]
    assert dropped[0].unknown_ids == (invented,)

    # The rendered report says so, rather than quietly omitting the sentences.
    document.claims = kept
    document.citations_dropped = dropped
    html = render_html(document)
    assert "2 claim(s) were removed from the summary" in html
    assert invented in html
    assert "A sentence mixing both." in html


def test_limitations_list_the_forced_scanner_failure(audit):
    session = get_session_factory()()
    try:
        document = build_report(session, audit["audit_id"])
    finally:
        session.close()

    limitations = "\n".join(document.limitations)
    assert "Scanner scancode did not complete" in limitations
    assert "licence database unavailable" in limitations
    assert "insufficient evidence" in limitations
    # The unresolved finding is named by id, so a reader can look it up.
    assert str(audit["findings"][-1].id) in limitations

    html = render_html(document)
    assert "Limitations" in html
    assert "Scanner scancode did not complete" in html


def test_the_sections_are_all_present(audit):
    session = get_session_factory()()
    try:
        document = build_report(session, audit["audit_id"])
    finally:
        session.close()

    assert document.audit.status == "completed"
    assert document.risk is not None
    assert document.risk.level == "critical"
    assert document.risk.rule == "severity_floor"
    assert document.risk.rationale
    assert [f.severity for f in document.top_findings][:2] == ["critical", "high"]
    assert {row.category for row in document.categories} == {"security", "dependency", "license"}
    assert document.categories[0].category == "security"
    assert document.priorities[0].severity == "critical"
    assert document.review is not None
    assert document.review.decision == "approved"
    assert document.review.reason == "Accepted the critical assessment."
    assert document.totals["critical"] == 1

    html = render_html(document)
    for heading in (
        "Executive summary",
        "Overall risk",
        "Top findings",
        "Category breakdown",
        "Remediation priorities",
        "Limitations",
        "Reviewer decision",
    ):
        assert heading in html


def test_an_audit_without_a_review_has_no_reviewer_block(audit):
    session = get_session_factory()()
    try:
        # Deleting the review cascades into append-only review_decisions; step around
        # the trigger for this fixture only.
        session.execute(text("ALTER TABLE review_decisions DISABLE TRIGGER USER"))
        for review in session.query(Review).filter(Review.audit_id == audit["audit_id"]):
            session.delete(review)
        session.commit()
        session.execute(text("ALTER TABLE review_decisions ENABLE TRIGGER USER"))
        session.commit()
        document = build_report(session, audit["audit_id"])
    finally:
        session.close()

    assert document.review is None
    assert "Reviewer decision" not in render_html(document)


def test_the_reporter_only_uses_what_is_stored(audit):
    """The summary is derived from the finding rows, not from anything else."""
    session = get_session_factory()()
    try:
        document = build_report(session, audit["audit_id"])
    finally:
        session.close()

    ids = [str(f.id) for f in audit["findings"]]
    assert write_summary(document.top_findings)  # sentences exist for what is stored
    cited = {fid for claim in document.claims for fid in claim.finding_ids}
    assert cited <= set(ids)
    assert any("critical" in claim.text.lower() for claim in document.claims)


def test_the_html_endpoint_serves_the_report(client, audit):
    response = client.get(f"/api/v1/audits/{audit['audit_id']}/report")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "Due diligence report" in body
    assert "Executive summary" in body
    assert str(audit["findings"][0].id) in body


def test_an_unknown_audit_is_404(client):
    response = client.get(f"/api/v1/audits/{uuid.uuid4()}/report")

    assert response.status_code == 404


def test_the_pdf_endpoint_exports_or_reports_why_it_cannot(client, audit):
    response = client.get(f"/api/v1/audits/{audit['audit_id']}/report.pdf")

    if response.status_code == 200:
        assert response.headers["content-type"] == "application/pdf"
        assert response.content.startswith(b"%PDF")
    else:
        # Documented limitation: WeasyPrint needs system libraries the host lacks.
        assert response.status_code == 503
        assert "WeasyPrint" in response.json()["detail"]
