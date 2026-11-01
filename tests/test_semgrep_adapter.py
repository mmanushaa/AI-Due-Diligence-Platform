"""Step 1.5: the Semgrep adapter against the benchmark fixtures.

The image must exist locally (see README). Tests are skipped otherwise.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.db.enums import AuditStatus, SourceType
from app.db.models import Audit, Evidence, Finding, Repository, ScannerRun
from app.db.session import get_session_factory
from app.scanners.semgrep import SEMGREP_IMAGE, SemgrepConfig, map_severity, scan_audit

FIXTURES = Path(__file__).resolve().parents[1] / "benchmark" / "repos"
DOCKER = shutil.which("docker")
MISSING_IMAGE = "due-diligence-semgrep:does-not-exist"


@pytest.fixture(scope="module", autouse=True)
def _drop_stale_repositories():
    """A killed run can leave rows behind; they would skew other modules' counts."""
    with get_session_factory()() as session:
        session.execute(delete(Repository).where(Repository.name.like("pytest-semgrep-%")))
        session.commit()
    yield


def _image_present(image: str) -> bool:
    if DOCKER is None:
        return False
    return subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, check=False
    ).returncode == 0


requires_scanner = pytest.mark.skipif(
    not _image_present(SEMGREP_IMAGE), reason=f"docker image {SEMGREP_IMAGE} is not available"
)


def _audit_for(session, snapshot: Path, name: str) -> tuple[Repository, Audit]:
    repository = Repository(name=name, source_type=SourceType.ZIP, storage_path=str(snapshot))
    session.add(repository)
    session.commit()
    session.refresh(repository)

    audit = Audit(repository_id=repository.id, status=AuditStatus.RUNNING)
    session.add(audit)
    session.commit()
    session.refresh(audit)
    return repository, audit


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("CRITICAL", "critical"),
        ("ERROR", "high"),
        ("WARNING", "medium"),
        ("INFO", "info"),
        ("warning", "medium"),
        ("UNKNOWN", "medium"),
        ("", "medium"),
    ],
)
def test_severity_mapping(raw, expected):
    assert map_severity(raw).value == expected


def _cleanup(session, repository, audit) -> None:
    session.delete(audit)  # cascades findings, evidence and scanner_runs
    session.commit()
    session.delete(repository)
    session.commit()


@requires_scanner
def test_python_sql_string_formatting_is_detected(tmp_path):
    with get_session_factory()() as session:
        repository, audit = _audit_for(session, FIXTURES / "python_app", "pytest-semgrep-py")
        try:
            summary = scan_audit(
                session, audit.id, FIXTURES / "python_app", output_root=tmp_path / "out"
            )

            assert summary.status.value == "succeeded"
            assert summary.detected == 1 and summary.stored == 1

            finding = session.execute(
                select(Finding).where(Finding.audit_id == audit.id)
            ).scalar_one()
            assert finding.category.value == "security"
            assert finding.origin.value == "scanner"
            assert finding.verification_status.value == "scanner_confirmed"
            assert finding.severity.value == "high"
            assert finding.verdict.value == "issue"
            assert finding.source_tool == "due-diligence.python.sql-string-formatting"

            evidence = session.execute(
                select(Evidence).where(Evidence.finding_id == finding.id)
            ).scalar_one()
            assert evidence.file_path == "app/database.py"
            assert (evidence.line_start, evidence.line_end) == (38, 38)
            assert "SELECT" in evidence.snippet
            assert evidence.kind.value == "primary"

            run = session.get(ScannerRun, summary.scanner_run_id)
            assert run.tool.value == "semgrep"
            assert run.status.value == "succeeded"
            assert run.version == "1.182.1"
            assert run.error is None
            assert run.raw_output_path and Path(run.raw_output_path).is_file()
            assert run.started_at is not None and run.finished_at is not None
        finally:
            _cleanup(session, repository, audit)


@requires_scanner
def test_js_sql_template_interpolation_is_detected(tmp_path):
    with get_session_factory()() as session:
        repository, audit = _audit_for(session, FIXTURES / "js_app", "pytest-semgrep-js")
        try:
            summary = scan_audit(session, audit.id, FIXTURES / "js_app", output_root=tmp_path / "out")
            assert summary.stored == 1

            finding = session.execute(
                select(Finding).where(Finding.audit_id == audit.id)
            ).scalar_one()
            assert finding.category.value == "security"
            assert finding.source_tool == "due-diligence.javascript.sql-template-interpolation"
            assert finding.severity.value == "high"

            evidence = session.execute(
                select(Evidence).where(Evidence.finding_id == finding.id)
            ).scalar_one()
            assert evidence.file_path == "src/db/index.js"
            assert (evidence.line_start, evidence.line_end) == (17, 17)
            assert "SELECT" in evidence.snippet
            assert "${email}" in evidence.snippet
        finally:
            _cleanup(session, repository, audit)


@requires_scanner
def test_rescanning_does_not_duplicate_findings(tmp_path):
    with get_session_factory()() as session:
        repository, audit = _audit_for(session, FIXTURES / "js_app", "pytest-semgrep-twice")
        try:
            first = scan_audit(session, audit.id, FIXTURES / "js_app", output_root=tmp_path / "out")
            second = scan_audit(session, audit.id, FIXTURES / "js_app", output_root=tmp_path / "out")

            assert (first.detected, first.stored) == (1, 1)
            assert (second.detected, second.stored) == (1, 0)
            assert len(
                session.execute(
                    select(Finding).where(Finding.audit_id == audit.id)
                ).scalars().all()
            ) == 1
        finally:
            _cleanup(session, repository, audit)


@pytest.mark.skipif(DOCKER is None, reason="docker is not installed")
def test_scanner_failure_is_recorded(tmp_path):
    with get_session_factory()() as session:
        repository, audit = _audit_for(session, FIXTURES / "python_app", "pytest-semgrep-fail")
        try:
            summary = scan_audit(
                session,
                audit.id,
                FIXTURES / "python_app",
                config=SemgrepConfig(image=MISSING_IMAGE, timeout_seconds=30),
                output_root=tmp_path / "out",
            )

            assert summary.status.value == "failed"
            assert summary.stored == 0
            assert summary.error

            run = session.get(ScannerRun, summary.scanner_run_id)
            assert run.status.value == "failed"
            assert run.error and "exited with" in run.error
            assert run.finished_at is not None
        finally:
            _cleanup(session, repository, audit)
