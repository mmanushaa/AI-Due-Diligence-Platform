"""Step 1.6: the OSV-Scanner adapter against the benchmark fixtures.

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
from app.scanners.osv import (
    OSV_IMAGE,
    OsvConfig,
    map_severity,
    scan_audit,
    write_synthetic_npm_lockfiles,
)

FIXTURES = Path(__file__).resolve().parents[1] / "benchmark" / "repos"
DOCKER = shutil.which("docker")
MISSING_IMAGE = "due-diligence-osv:does-not-exist"


@pytest.fixture(scope="module", autouse=True)
def _drop_stale_repositories():
    """A killed run can leave rows behind; they would skew other modules' counts."""
    with get_session_factory()() as session:
        session.execute(delete(Repository).where(Repository.name.like("pytest-osv-%")))
        session.commit()
    yield


def _image_present(image: str) -> bool:
    if DOCKER is None:
        return False
    return subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, check=False
    ).returncode == 0


requires_scanner = pytest.mark.skipif(
    not _image_present(OSV_IMAGE), reason=f"docker image {OSV_IMAGE} is not available"
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


def _cleanup(session, repository, audit) -> None:
    session.delete(audit)  # cascades findings, evidence and scanner_runs
    session.commit()
    session.delete(repository)
    session.commit()


@pytest.mark.parametrize(
    ("rating", "score", "expected"),
    [
        ("CRITICAL", None, "critical"),
        ("HIGH", None, "high"),
        ("MODERATE", None, "medium"),
        ("MEDIUM", None, "medium"),
        ("LOW", None, "low"),
        ("high", None, "high"),
        (None, 9.8, "critical"),
        (None, 7.5, "high"),
        (None, 5.3, "medium"),
        (None, 2.1, "low"),
        (None, None, "medium"),
        ("UNKNOWN", None, "medium"),
    ],
)
def test_severity_mapping(rating, score, expected):
    assert map_severity(rating, score).value == expected


def test_exact_pins_become_a_temporary_lockfile(tmp_path):
    mapping = write_synthetic_npm_lockfiles(FIXTURES / "js_app", tmp_path)

    assert mapping == {"package-lock.json": "package.json"}
    payload = (tmp_path / "package-lock.json").read_text(encoding="utf-8")
    assert '"lodash": "4.17.15"' in payload
    assert "node_modules/lodash" in payload
    # Ranges cannot name an installed version, so nothing is invented for them.
    assert write_synthetic_npm_lockfiles(FIXTURES / "python_app", tmp_path / "other") == {}


@requires_scanner
def test_python_pinned_vulnerable_dependency_is_detected(tmp_path):
    with get_session_factory()() as session:
        repository, audit = _audit_for(session, FIXTURES / "python_app", "pytest-osv-py")
        try:
            summary = scan_audit(
                session, audit.id, FIXTURES / "python_app", output_root=tmp_path / "out"
            )

            assert summary.status.value == "succeeded"
            assert summary.detected == summary.stored
            assert summary.detected >= 1

            finding = session.execute(
                select(Finding).where(
                    Finding.audit_id == audit.id,
                    Finding.source_tool == "osv:GHSA-g3rq-g295-4j3m",
                )
            ).scalar_one()
            assert finding.category.value == "dependency"
            assert finding.origin.value == "scanner"
            assert finding.verification_status.value == "scanner_confirmed"
            assert finding.severity.value == "medium"
            assert finding.verdict.value == "issue"
            assert finding.title.startswith("jinja2 2.11.2")
            assert "2.11.2" in finding.reasoning
            assert "jinja2 to 2.11.3" in finding.recommendation
            assert "CVE-2020-28493" in finding.reasoning

            evidence = session.execute(
                select(Evidence).where(Evidence.finding_id == finding.id)
            ).scalar_one()
            assert evidence.file_path == "requirements.txt"
            assert (evidence.line_start, evidence.line_end) == (3, 3)
            assert evidence.snippet == "jinja2==2.11.2"
            assert evidence.kind.value == "primary"

            run = session.get(ScannerRun, summary.scanner_run_id)
            assert run.tool.value == "osv"
            assert run.status.value == "succeeded"
            assert run.version == "2.7.0"
            assert run.error is None
            assert run.raw_output_path and Path(run.raw_output_path).is_file()
            assert run.started_at is not None and run.finished_at is not None
        finally:
            _cleanup(session, repository, audit)


@requires_scanner
def test_js_pinned_vulnerable_dependency_is_detected(tmp_path):
    with get_session_factory()() as session:
        repository, audit = _audit_for(session, FIXTURES / "js_app", "pytest-osv-js")
        try:
            summary = scan_audit(session, audit.id, FIXTURES / "js_app", output_root=tmp_path / "out")
            assert summary.stored >= 1

            finding = session.execute(
                select(Finding).where(
                    Finding.audit_id == audit.id,
                    Finding.source_tool == "osv:GHSA-p6mc-m468-83gw",
                )
            ).scalar_one()
            assert finding.category.value == "dependency"
            assert finding.severity.value == "high"
            assert "lodash 4.17.15" in finding.title
            assert "lodash to 4.17.19" in finding.recommendation
            assert "CVE-2020-8203" in finding.reasoning

            evidence = session.execute(
                select(Evidence).where(Evidence.finding_id == finding.id)
            ).scalar_one()
            assert evidence.file_path == "package.json"
            assert (evidence.line_start, evidence.line_end) == (12, 12)
            assert "lodash" in evidence.snippet
        finally:
            _cleanup(session, repository, audit)


@requires_scanner
def test_rescanning_does_not_duplicate_findings(tmp_path):
    with get_session_factory()() as session:
        repository, audit = _audit_for(session, FIXTURES / "js_app", "pytest-osv-twice")
        try:
            first = scan_audit(session, audit.id, FIXTURES / "js_app", output_root=tmp_path / "out")
            second = scan_audit(session, audit.id, FIXTURES / "js_app", output_root=tmp_path / "out")

            assert first.stored >= 1
            assert second.stored == 0
            assert len(
                session.execute(
                    select(Finding).where(Finding.audit_id == audit.id)
                ).scalars().all()
            ) == first.stored
        finally:
            _cleanup(session, repository, audit)


@pytest.mark.skipif(DOCKER is None, reason="docker is not installed")
def test_scanner_failure_is_recorded(tmp_path):
    with get_session_factory()() as session:
        repository, audit = _audit_for(session, FIXTURES / "python_app", "pytest-osv-fail")
        try:
            summary = scan_audit(
                session,
                audit.id,
                FIXTURES / "python_app",
                config=OsvConfig(image=MISSING_IMAGE, timeout_seconds=60),
                output_root=tmp_path / "out",
            )

            assert summary.status.value == "failed"
            assert summary.stored == 0
            assert summary.error

            run = session.get(ScannerRun, summary.scanner_run_id)
            assert run.tool.value == "osv"
            assert run.status.value == "failed"
            assert run.error and "exited with" in run.error
            assert run.finished_at is not None
        finally:
            _cleanup(session, repository, audit)
