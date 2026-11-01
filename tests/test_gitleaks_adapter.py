"""Step 1.4: the Gitleaks adapter against the benchmark fixtures.

These tests execute the scanner image through subprocess. They are skipped when
docker or the image is unavailable.
"""

import logging
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.db.enums import AuditStatus, SourceType
from app.db.models import Audit, Evidence, Finding, Repository, ScannerRun
from app.db.session import get_session_factory
from app.scanners.gitleaks import GITLEAKS_IMAGE, GitleaksConfig, mask_secret, scan_audit

FIXTURES = Path(__file__).resolve().parents[1] / "benchmark" / "repos"

DOCKER = shutil.which("docker")
MISSING_IMAGE = "due-diligence-gitleaks:does-not-exist"


@pytest.fixture(scope="module", autouse=True)
def _drop_stale_repositories():
    """A killed run can leave rows behind; they would skew other modules' counts."""
    with get_session_factory()() as session:
        session.execute(delete(Repository).where(Repository.name.like("pytest-gitleaks-%")))
        session.commit()
    yield


def _image_present(image: str) -> bool:
    if DOCKER is None:
        return False
    return subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, check=False
    ).returncode == 0


requires_scanner = pytest.mark.skipif(
    not _image_present(GITLEAKS_IMAGE),
    reason=f"docker image {GITLEAKS_IMAGE} is not available",
)


def fixture_token(relative_path: str) -> str:
    text = (FIXTURES / relative_path).read_text(encoding="utf-8")
    match = re.search(r"ghp_[A-Za-z0-9]{36}", text)
    assert match, f"fixture {relative_path} no longer contains a planted token"
    return match.group(0)


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


def _cleanup(session, repository, audit) -> None:
    session.delete(audit)  # cascades findings, evidence and scanner_runs
    session.commit()
    session.delete(repository)
    session.commit()


@requires_scanner
def test_python_fixture_secret_is_detected_and_normalized(tmp_path):
    token = fixture_token("python_app/app/config.py")

    with get_session_factory()() as session:
        repository, audit = _audit_for(session, FIXTURES / "python_app", "pytest-gitleaks-py")
        try:
            summary = scan_audit(
                session, audit.id, FIXTURES / "python_app", output_root=tmp_path / "out"
            )

            assert summary.status.value == "succeeded"
            assert summary.detected == 1 and summary.stored == 1

            findings = session.execute(
                select(Finding).where(Finding.audit_id == audit.id)
            ).scalars().all()
            assert len(findings) == 1
            finding = findings[0]
            assert finding.category.value == "secret"
            assert finding.origin.value == "scanner"
            assert finding.verification_status.value == "scanner_confirmed"
            assert finding.severity.value == "high"
            assert finding.verdict.value == "issue"
            assert finding.source_tool == "gitleaks:github-pat"
            assert len(finding.fingerprint) == 64

            evidence = session.execute(
                select(Evidence).where(Evidence.finding_id == finding.id)
            ).scalars().all()
            assert len(evidence) == 1
            assert evidence[0].file_path == "app/config.py"
            assert (evidence[0].line_start, evidence[0].line_end) == (16, 16)
            assert evidence[0].kind.value == "primary"

            run = session.get(ScannerRun, summary.scanner_run_id)
            assert run.status.value == "succeeded"
            assert run.version == "8.28.0"
            assert run.error is None
            assert run.started_at is not None and run.finished_at is not None
            assert run.raw_output_path and Path(run.raw_output_path).is_file()

            # The stored snippet shows the masked value, not the secret.
            assert token not in evidence[0].snippet
            assert "REDACTED" in evidence[0].snippet
            assert mask_secret(token).startswith("ghp_")
        finally:
            _cleanup(session, repository, audit)


@requires_scanner
def test_js_fixture_secret_is_detected_at_the_expected_lines(tmp_path):
    with get_session_factory()() as session:
        repository, audit = _audit_for(session, FIXTURES / "js_app", "pytest-gitleaks-js")
        try:
            summary = scan_audit(session, audit.id, FIXTURES / "js_app", output_root=tmp_path / "out")
            assert summary.stored == 1

            finding = session.execute(
                select(Finding).where(Finding.audit_id == audit.id)
            ).scalar_one()
            evidence = session.execute(
                select(Evidence).where(Evidence.finding_id == finding.id)
            ).scalar_one()
            assert evidence.file_path == "src/config.js"
            assert (evidence.line_start, evidence.line_end) == (7, 7)
        finally:
            _cleanup(session, repository, audit)


@requires_scanner
def test_plaintext_secret_never_reaches_database_logs_or_report(tmp_path, caplog):
    py_token = fixture_token("python_app/app/config.py")
    js_token = fixture_token("js_app/src/config.js")

    with caplog.at_level(logging.DEBUG), get_session_factory()() as session:
        repository, audit = _audit_for(session, FIXTURES / "python_app", "pytest-gitleaks-secret")
        try:
            scan_audit(session, audit.id, FIXTURES / "python_app", output_root=tmp_path / "out")

            dump = []
            for finding in session.execute(
                select(Finding).where(Finding.audit_id == audit.id)
            ).scalars():
                dump += [
                    finding.title, finding.reasoning or "", finding.business_impact or "",
                    finding.recommendation or "", finding.source_tool or "",
                    finding.fingerprint,
                ]
            for evidence in session.execute(select(Evidence)).scalars():
                dump += [evidence.file_path, evidence.snippet or ""]
            for run in session.execute(
                select(ScannerRun).where(ScannerRun.audit_id == audit.id)
            ).scalars():
                dump += [run.version or "", run.error or "", run.raw_output_path or ""]
                if run.raw_output_path:
                    dump.append(Path(run.raw_output_path).read_text(encoding="utf-8"))

            stored_text = "\n".join(dump)
            assert py_token not in stored_text
            assert js_token not in stored_text
            assert py_token not in caplog.text
            assert "REDACTED" in stored_text
        finally:
            _cleanup(session, repository, audit)


@requires_scanner
def test_rescanning_the_same_audit_does_not_duplicate_findings(tmp_path):
    with get_session_factory()() as session:
        repository, audit = _audit_for(session, FIXTURES / "python_app", "pytest-gitleaks-twice")
        try:
            first = scan_audit(session, audit.id, FIXTURES / "python_app", output_root=tmp_path / "out")
            second = scan_audit(session, audit.id, FIXTURES / "python_app", output_root=tmp_path / "out")

            assert (first.detected, first.stored) == (1, 1)
            assert (second.detected, second.stored) == (1, 0)
            count = len(
                session.execute(
                    select(Finding).where(Finding.audit_id == audit.id)
                ).scalars().all()
            )
            assert count == 1
        finally:
            _cleanup(session, repository, audit)


@pytest.mark.skipif(DOCKER is None, reason="docker is not installed")
def test_scanner_failure_is_recorded_and_returns_cleanly(tmp_path):
    with get_session_factory()() as session:
        repository, audit = _audit_for(session, FIXTURES / "python_app", "pytest-gitleaks-fail")
        try:
            summary = scan_audit(
                session,
                audit.id,
                FIXTURES / "python_app",
                config=GitleaksConfig(image=MISSING_IMAGE, timeout_seconds=30),
                output_root=tmp_path / "out",
            )

            assert summary.status.value == "failed"
            assert summary.stored == 0
            assert summary.error

            run = session.get(ScannerRun, summary.scanner_run_id)
            assert run.status.value == "failed"
            assert run.error and "exited with" in run.error
            assert run.finished_at is not None
            assert session.execute(
                select(Finding).where(Finding.audit_id == audit.id)
            ).first() is None
        finally:
            _cleanup(session, repository, audit)
