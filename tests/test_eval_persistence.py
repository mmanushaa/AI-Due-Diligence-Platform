"""Step 2.6b-B: what the harness measured is what the database keeps.

These tests run against the real Postgres database and go through
``_persist_run``, the same function ``run_configuration`` calls, so a column that
is wired up wrongly fails here rather than silently writing NULL during a
benchmark run. They clean up every row they create.
"""

from __future__ import annotations

import uuid
from dataclasses import fields
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete, text

from app.db.enums import (
    AuditStatus,
    Category,
    EvidenceKind,
    FindingOrigin,
    Severity,
    SourceType,
    VerificationStatus,
)
from app.db.models import Audit, Evidence, Finding, Repository
from app.db.session import get_session_factory
from benchmark.harness import configs, metrics
from benchmark.harness.runner import (
    BENCHMARK_VERSION,
    MISS_NO_MATCH,
    ExtraFinding,
    IssueOutcome,
    RepoEvaluation,
    _persist_run,
)

MATCHED_ISSUE = "py-sqli-01"
MISSED_ISSUE = "py-authz-02"
REPO = "python_app"
MODEL = "gpt-4o-mini"
SEMGREP_RULE = "python.lang.security.audit.formatted-sql-query"
COST = Decimal("0.0431")
RUNTIME_MS = 2345


@pytest.fixture
def stored_findings():
    """One matched finding and one extra, with the evidence rows they cite."""
    factory = get_session_factory()
    session = factory()
    try:
        repository = Repository(
            name=f"pytest-eval-{uuid.uuid4().hex[:8]}",
            source_type=SourceType.ZIP,
            storage_path="benchmark/repos/python_app",
        )
        session.add(repository)
        session.flush()

        audit = Audit(repository_id=repository.id, status=AuditStatus.COMPLETED)
        session.add(audit)
        session.flush()

        matched = Finding(
            audit_id=audit.id,
            category=Category.SECURITY,
            title="SQL built with string formatting",
            severity=Severity.HIGH,
            origin=FindingOrigin.SCANNER,
            source_tool=SEMGREP_RULE,
            verification_status=VerificationStatus.SCANNER_CONFIRMED,
            fingerprint=f"fp-matched-{uuid.uuid4().hex[:8]}",
        )
        extra = Finding(
            audit_id=audit.id,
            category=Category.SECURITY,
            title="Something nobody planted",
            severity=Severity.LOW,
            origin=FindingOrigin.AGENT,
            source_tool="security_crew",
            verification_status=VerificationStatus.UNRESOLVED,
            fingerprint=f"fp-extra-{uuid.uuid4().hex[:8]}",
        )
        session.add_all([matched, extra])
        session.flush()

        session.add_all(
            [
                Evidence(
                    finding_id=matched.id,
                    file_path="app/database.py",
                    line_start=30,
                    line_end=32,
                    kind=EvidenceKind.PRIMARY,
                ),
                Evidence(
                    finding_id=extra.id,
                    file_path="app/other.py",
                    line_start=7,
                    line_end=9,
                    kind=EvidenceKind.PRIMARY,
                ),
            ]
        )
        session.commit()
        audit_id, repository_id = audit.id, repository.id
        finding_ids = (matched.id, extra.id)
    finally:
        session.close()

    yield finding_ids

    with factory() as cleanup:
        cleanup.execute(delete(Finding).where(Finding.audit_id == audit_id))
        cleanup.execute(delete(Audit).where(Audit.id == audit_id))
        cleanup.execute(delete(Repository).where(Repository.id == repository_id))
        cleanup.commit()


def _evaluation(matched_id: uuid.UUID, extra_id: uuid.UUID) -> RepoEvaluation:
    return RepoEvaluation(
        score=metrics.RepoScore(
            repo=REPO,
            expected=2,
            findings=2,
            matched=1,
            duplicates=0,
            evidence_correct=1,
            severity_correct=1,
            cross_file_expected=1,
            cross_file_matched=1,
            cost_usd=COST,
            runtime_ms=RUNTIME_MS,
        ),
        outcomes=(
            IssueOutcome(
                issue_id=MATCHED_ISSUE,
                repo=REPO,
                detected=True,
                evidence_correct=True,
                severity_correct=True,
                matched_finding_id=matched_id,
                matched_origin="scanner",
                matched_source_tool=SEMGREP_RULE,
                matched_file="app/database.py",
                matched_line_start=30,
                matched_line_end=32,
                is_cross_file=True,
                miss_reason=None,
            ),
            IssueOutcome(
                issue_id=MISSED_ISSUE,
                repo=REPO,
                detected=False,
                is_cross_file=False,
                miss_reason=MISS_NO_MATCH,
            ),
        ),
        extras=(
            ExtraFinding(
                finding_id=extra_id,
                repo=REPO,
                category="security",
                title="Something nobody planted",
                file="app/other.py",
                line_start=7,
                line_end=9,
                origin="agent",
                source_tool="security_crew",
                rejection_reason=None,
            ),
        ),
        agent_models=(MODEL,),
    )


def test_a_persisted_run_keeps_every_instrumented_column(stored_findings):
    matched_id, extra_id = stored_findings
    evaluation = _evaluation(matched_id, extra_id)
    factory = get_session_factory()

    session = factory()
    try:
        eval_run = _persist_run(
            session,
            configuration=configs.get("single_agent"),
            benchmark_version=BENCHMARK_VERSION,
            manifest_version="0.5.0",
            evaluations=(evaluation,),
            total=metrics.totals((evaluation.score,)),
            started_at=datetime.now(timezone.utc),
        )
        run_id = eval_run.id
    finally:
        session.close()

    try:
        with factory() as fresh:
            rows = (
                fresh.execute(
                    text(
                        "SELECT expected_issue_id, detected, cost_usd, latency_ms,"
                        " matched_finding_id, matched_origin, matched_source_tool,"
                        " matched_file, matched_line_start, matched_line_end,"
                        " is_cross_file, miss_reason"
                        " FROM eval_results WHERE eval_run_id = :run"
                        " ORDER BY expected_issue_id"
                    ),
                    {"run": run_id},
                )
                .mappings()
                .all()
            )
            extras = (
                fresh.execute(
                    text(
                        "SELECT finding_id, repo, category, title, file, line_start,"
                        " line_end, origin, source_tool, rejection_reason"
                        " FROM eval_extras WHERE eval_run_id = :run"
                    ),
                    {"run": run_id},
                )
                .mappings()
                .all()
            )
            stored_metrics = fresh.execute(
                text("SELECT metrics FROM eval_runs WHERE id = :run"), {"run": run_id}
            ).scalar_one()

        assert len(rows) == 2

        # Ordered by expected_issue_id: the missed issue sorts before the matched one.
        missed, matched = rows[0], rows[1]
        assert missed["expected_issue_id"] == MISSED_ISSUE
        assert missed["detected"] is False
        assert missed["matched_finding_id"] is None
        assert missed["matched_origin"] is None
        assert missed["matched_source_tool"] is None
        assert missed["matched_file"] is None
        assert missed["matched_line_start"] is None
        assert missed["matched_line_end"] is None
        assert missed["is_cross_file"] is False
        assert missed["miss_reason"] == MISS_NO_MATCH

        assert matched["expected_issue_id"] == MATCHED_ISSUE
        assert matched["detected"] is True
        assert matched["matched_finding_id"] == matched_id
        assert matched["matched_origin"] == "scanner"
        assert matched["matched_source_tool"] == SEMGREP_RULE
        assert matched["matched_file"] == "app/database.py"
        assert matched["matched_line_start"] == 30
        assert matched["matched_line_end"] == 32
        assert matched["is_cross_file"] is True
        assert matched["miss_reason"] is None

        # Cost and latency are the repository's, on every row.
        for row in rows:
            assert row["cost_usd"] == COST
            assert row["latency_ms"] == RUNTIME_MS

        assert len(extras) == 1
        extra = extras[0]
        assert extra["finding_id"] == extra_id
        assert extra["repo"] == REPO
        assert extra["category"] == "security"
        assert extra["title"] == "Something nobody planted"
        assert extra["file"] == "app/other.py"
        assert extra["line_start"] == 7
        assert extra["line_end"] == 9
        assert extra["origin"] == "agent"
        assert extra["source_tool"] == "security_crew"
        # No pipeline step produces a rejection reason yet, so it stays NULL.
        assert extra["rejection_reason"] is None

        assert stored_metrics["models"] == {REPO: [MODEL]}
    finally:
        with factory() as cleanup:
            cleanup.execute(text("DELETE FROM eval_runs WHERE id = :run"), {"run": run_id})
            cleanup.commit()


def test_every_matched_finding_id_points_at_a_real_finding(stored_findings):
    """The check a reader runs by hand, kept as a test."""
    matched_id, extra_id = stored_findings
    evaluation = _evaluation(matched_id, extra_id)
    factory = get_session_factory()

    session = factory()
    try:
        run_id = _persist_run(
            session,
            configuration=configs.get("single_agent"),
            benchmark_version=BENCHMARK_VERSION,
            manifest_version="0.5.0",
            evaluations=(evaluation,),
            total=metrics.totals((evaluation.score,)),
            started_at=datetime.now(timezone.utc),
        ).id
    finally:
        session.close()

    try:
        with factory() as fresh:
            orphans = fresh.execute(
                text(
                    "SELECT count(*) FROM eval_results"
                    " WHERE matched_finding_id IS NOT NULL"
                    " AND matched_finding_id NOT IN (SELECT id FROM findings)"
                )
            ).scalar_one()
        assert orphans == 0
    finally:
        with factory() as cleanup:
            cleanup.execute(text("DELETE FROM eval_runs WHERE id = :run"), {"run": run_id})
            cleanup.commit()


def test_issue_outcome_keeps_the_instrumented_fields():
    """Fails if a field is dropped from IssueOutcome, which is how NULLs crept in."""
    names = {item.name for item in fields(IssueOutcome)}
    assert {
        "matched_finding_id",
        "matched_origin",
        "matched_source_tool",
        "matched_file",
        "matched_line_start",
        "matched_line_end",
        "is_cross_file",
        "miss_reason",
    } <= names


def test_every_extra_finding_field_survives_the_record():
    names = {item.name for item in fields(ExtraFinding)}
    assert {
        "finding_id",
        "repo",
        "category",
        "title",
        "file",
        "line_start",
        "line_end",
        "origin",
        "source_tool",
        "rejection_reason",
    } <= names
