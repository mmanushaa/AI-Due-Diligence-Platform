"""Step 3.9 demo: build a report from stored rows and write it to ``artifacts/``.

    python -m scripts.demo_report <audit-uuid>          # a real audit
    python -m scripts.demo_report --demo                # seeded example
    python -m scripts.demo_report --demo --pdf          # also try the PDF

``--demo`` creates an audit with findings of every useful shape (verified, scanner
confirmed, unresolved/insufficient evidence, rejected false positive), a review
decision and a recorded scanner failure, so the report's sections have something to
show. It writes to the database; use it on a development database.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

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
from app.reports import PdfUnavailable, build_report, render_html, render_pdf

OUT = Path(__file__).resolve().parents[1] / "artifacts"


def seed(session) -> uuid.UUID:
    repository = Repository(
        name="demo/payments-service",
        source_type=SourceType.ZIP,
        storage_path="/tmp/demo-payments-service",
    )
    session.add(repository)
    session.flush()
    audit = Audit(
        repository_id=repository.id,
        status=AuditStatus.COMPLETED,
        cost_usd=Decimal("0.0412"),
        budget_usd=Decimal("1.0000"),
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )
    session.add(audit)
    session.flush()

    rows = [
        ("SQL injection in the export route", Category.SECURITY, Severity.CRITICAL,
         VerificationStatus.VERIFIED, Verdict.ISSUE, "app/routes/export.py", 41),
        ("Debug mode enabled in production settings", Category.SECURITY, Severity.HIGH,
         VerificationStatus.VERIFIED, Verdict.ISSUE, "app/config.py", 18),
        ("Session cookie missing SameSite", Category.SECURITY, Severity.MEDIUM,
         VerificationStatus.SCANNER_CONFIRMED, Verdict.ISSUE, "app/auth.py", 77),
        ("GPL-licensed library in a proprietary bundle", Category.LICENSE, Severity.HIGH,
         VerificationStatus.UNRESOLVED, Verdict.INSUFFICIENT_EVIDENCE, "LICENSE", 1),
        ("Pinned dependency has a known CVE", Category.DEPENDENCY, Severity.MEDIUM,
         VerificationStatus.UNRESOLVED, Verdict.INSUFFICIENT_EVIDENCE, "requirements.txt", 12),
        ("Timing comparison flagged but constant-time helper is used", Category.SECURITY,
         Severity.LOW, VerificationStatus.REJECTED_FALSE_POSITIVE, Verdict.NO_ISSUE,
         "app/crypto.py", 96),
    ]
    findings = []
    for title, category, severity, status, verdict, path, line in rows:
        finding = Finding(
            audit_id=audit.id,
            category=category,
            title=title,
            severity=severity,
            confidence=Decimal("0.82"),
            origin=FindingOrigin.SCANNER,
            source_tool="semgrep",
            verification_status=status,
            verdict=verdict,
            recommendation=f"Address: {title}",
            fingerprint=uuid.uuid4().hex,
        )
        session.add(finding)
        session.flush()
        session.add(
            Evidence(
                finding_id=finding.id,
                file_path=path,
                line_start=line,
                line_end=line + 2,
                snippet="...",
                kind=EvidenceKind.PRIMARY,
            )
        )
        findings.append(finding)

    assessment = RiskAssessment(
        audit_id=audit.id,
        overall_level=Severity.CRITICAL,
        overall_score=Decimal("0.9300"),
        rule_applied="severity_floor",
        rationale="Highest non-rejected finding severity is critical.",
        status=AssessmentStatus.FINAL,
    )
    session.add(assessment)
    session.flush()
    review = Review(
        audit_id=audit.id,
        assessment_id=assessment.id,
        status=ReviewStatus.APPROVED,
        requested_at=datetime.now(timezone.utc),
    )
    session.add(review)
    session.flush()
    session.add(
        ReviewDecisionRow(
            review_id=review.id,
            reviewer_id=None,
            decision=ReviewDecision.APPROVE,
            reason="SQL injection reproduced by hand; critical stands. Licence question "
                   "stays open until legal answers.",
            original_level=Severity.CRITICAL,
            final_level=Severity.CRITICAL,
        )
    )
    session.add(
        ScannerFailure(
            audit_id=audit.id,
            tool="scancode",
            reason="scanner container exited with status 1: licence database unavailable",
        )
    )
    session.commit()
    return audit.id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit_id", nargs="?")
    parser.add_argument("--demo", action="store_true", help="seed a demo audit first")
    parser.add_argument("--pdf", action="store_true", help="also write the PDF")
    args = parser.parse_args()

    session = get_session_factory()()
    try:
        if args.demo:
            audit_id = seed(session)
        elif args.audit_id:
            audit_id = uuid.UUID(args.audit_id)
        else:
            audit_id = session.execute(
                select(Finding.audit_id)
                .group_by(Finding.audit_id)
                .order_by(Finding.audit_id)
                .limit(1)
            ).scalar_one()
        document = build_report(session, audit_id)
    finally:
        session.close()

    OUT.mkdir(exist_ok=True)
    html_path = OUT / f"report-{audit_id}.html"
    html = render_html(document)
    html_path.write_text(html, encoding="utf-8")
    print(f"audit      {audit_id}")
    print(f"repository {document.audit.repository}")
    print(f"risk       {document.risk.level if document.risk else '—'} "
          f"({document.risk.rule if document.risk else '—'})")
    print(f"findings   {sum(document.totals.values())} · claims {len(document.claims)} "
          f"· dropped {len(document.citations_dropped)}")
    for line in document.limitations:
        print(f"limitation {line}")
    print(f"html       {html_path}")

    if args.pdf:
        try:
            pdf_path = OUT / f"report-{audit_id}.pdf"
            pdf_path.write_bytes(render_pdf(html))
            print(f"pdf        {pdf_path}")
        except PdfUnavailable as exc:
            print(f"pdf        unavailable: {exc}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
