"""Step 1.8: parallel scanner orchestration, failure isolation and dedupe.

The docker-backed tests are skipped when a scanner image is missing (see README).
"""

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select

from app.db.enums import AuditStatus, ScannerRunStatus, ScannerTool, SourceType
from app.db.models import Audit, Finding, Repository, ScannerRun
from app.db.session import get_session_factory
from app.scanners.common import existing_fingerprints
from app.scanners.gitleaks import GITLEAKS_IMAGE, GitleaksConfig
from app.scanners.orchestrator import ScannerConfigs, run_scanners
from app.scanners.osv import OSV_IMAGE
from app.scanners.scancode import SCANCODE_IMAGE
from app.scanners.semgrep import SEMGREP_IMAGE

FIXTURES = Path(__file__).resolve().parents[1] / "benchmark" / "repos"
DOCKER = shutil.which("docker")
IMAGES = (GITLEAKS_IMAGE, SEMGREP_IMAGE, OSV_IMAGE, SCANCODE_IMAGE)
MISSING_IMAGE = "due-diligence-gitleaks:does-not-exist"


@pytest.fixture(scope="module", autouse=True)
def _drop_stale_repositories():
    """A killed run can leave rows behind; they would skew other modules' counts."""
    with get_session_factory()() as session:
        session.execute(delete(Repository).where(Repository.name.like("pytest-orch-%")))
        session.commit()
    yield


def _image_present(image: str) -> bool:
    if DOCKER is None:
        return False
    return subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, check=False
    ).returncode == 0


requires_scanners = pytest.mark.skipif(
    not all(_image_present(image) for image in IMAGES),
    reason="every scanner image is required for an orchestrated run",
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


def _fingerprint_counts(session, audit_id) -> tuple[int, int]:
    total, distinct = session.execute(
        select(func.count(Finding.id), func.count(func.distinct(Finding.fingerprint))).where(
            Finding.audit_id == audit_id
        )
    ).one()
    return total, distinct


def _outcome(result, tool: ScannerTool):
    return next(outcome for outcome in result.outcomes if outcome.tool is tool)


# --- dedupe gate (no docker) ------------------------------------------------


def test_existing_fingerprints_is_the_dedupe_gate():
    with get_session_factory()() as session:
        repository, audit = _audit_for(session, FIXTURES / "python_app", "pytest-orch-dedupe")
        try:
            finding = Finding(
                audit_id=audit.id,
                category="security",
                title="probe",
                severity="low",
                origin="scanner",
                verification_status="scanner_confirmed",
                verdict="issue",
                fingerprint="probe-fingerprint",
            )
            session.add(finding)
            session.commit()

            assert "probe-fingerprint" in existing_fingerprints(session, audit.id)
            assert existing_fingerprints(session, audit.id) == {"probe-fingerprint"}
        finally:
            _cleanup(session, repository, audit)


# --- failure isolation (no docker) -----------------------------------------


def test_unexpected_exception_is_contained_and_recorded():
    with get_session_factory()() as session:
        repository, audit = _audit_for(session, FIXTURES / "python_app", "pytest-orch-exception")
        try:

            def exploding(session, audit_id, snapshot_root, *, config, output_root):
                # The adapter had already opened its run row when it blew up.
                session.add(
                    ScannerRun(
                        audit_id=audit_id,
                        tool=ScannerTool.GITLEAKS,
                        status=ScannerRunStatus.RUNNING,
                        started_at=datetime.now(timezone.utc),
                    )
                )
                session.commit()
                raise RuntimeError("gitleaks exploded")

            def succeeding(session, audit_id, snapshot_root, *, config, output_root):
                return SimpleNamespace(
                    scanner_run_id=None,
                    status=ScannerRunStatus.SUCCEEDED,
                    detected=0,
                    stored=0,
                    error=None,
                )

            result = run_scanners(
                audit.id,
                FIXTURES / "python_app",
                scanners={
                    ScannerTool.GITLEAKS: exploding,
                    ScannerTool.SEMGREP: succeeding,
                },
            )

            assert [outcome.tool for outcome in result.outcomes] == [
                ScannerTool.GITLEAKS,
                ScannerTool.SEMGREP,
            ]
            failed = _outcome(result, ScannerTool.GITLEAKS)
            assert failed.status is ScannerRunStatus.FAILED
            assert failed.error == "RuntimeError: gitleaks exploded"
            assert result.failed == (failed,)
            assert _outcome(result, ScannerTool.SEMGREP).ok

            run = session.execute(
                select(ScannerRun).where(
                    ScannerRun.audit_id == audit.id,
                    ScannerRun.tool == ScannerTool.GITLEAKS,
                )
            ).scalar_one()
            assert run.status is ScannerRunStatus.FAILED
            assert "gitleaks exploded" in run.error
            assert run.finished_at is not None
        finally:
            _cleanup(session, repository, audit)


def test_failure_before_the_run_row_exists_still_records_one():
    with get_session_factory()() as session:
        repository, audit = _audit_for(session, FIXTURES / "python_app", "pytest-orch-early")
        try:

            def exploding(session, audit_id, snapshot_root, *, config, output_root):
                raise ValueError("inherited workers do not work here")

            result = run_scanners(
                audit.id,
                FIXTURES / "python_app",
                scanners={ScannerTool.SEMGREP: exploding},
            )

            assert len(result.outcomes) == 1
            assert result.outcomes[0].status is ScannerRunStatus.FAILED
            assert result.outcomes[0].scanner_run_id is not None

            run = session.get(ScannerRun, result.outcomes[0].scanner_run_id)
            assert run.status is ScannerRunStatus.FAILED
            assert run.error == "ValueError: inherited workers do not work here"
            assert run.started_at is not None and run.finished_at is not None
        finally:
            _cleanup(session, repository, audit)


# --- a whole orchestrated run (docker) -------------------------------------


@requires_scanners
def test_second_run_of_the_same_audit_creates_no_duplicates(tmp_path):
    with get_session_factory()() as session:
        repository, audit = _audit_for(session, FIXTURES / "python_app", "pytest-orch-twice")
        try:
            first = run_scanners(
                audit.id, FIXTURES / "python_app", output_root=tmp_path / "out"
            )
            assert first.all_succeeded, [outcome.error for outcome in first.failed]
            assert {outcome.tool for outcome in first.outcomes} == set(ScannerTool) - {
                ScannerTool.AST
            }

            # Every scanner contributed its category from the fixture.
            categories = set(
                session.execute(
                    select(Finding.category).where(Finding.audit_id == audit.id)
                ).scalars()
            )
            assert {"secret", "security", "dependency", "license"} <= categories

            total_first, distinct_first = _fingerprint_counts(session, audit.id)
            assert total_first == distinct_first > 0
            stored_first = {outcome.tool: outcome.stored for outcome in first.outcomes}
            assert all(stored > 0 for stored in stored_first.values())

            second = run_scanners(
                audit.id, FIXTURES / "python_app", output_root=tmp_path / "out"
            )
            assert second.all_succeeded
            assert {outcome.tool: outcome.stored for outcome in second.outcomes} == {
                tool: 0 for tool in stored_first
            }

            total_second, distinct_second = _fingerprint_counts(session, audit.id)
            assert total_second == total_first == distinct_second
        finally:
            _cleanup(session, repository, audit)


@requires_scanners
def test_one_failed_scanner_leaves_the_others_intact(tmp_path):
    with get_session_factory()() as session:
        repository, audit = _audit_for(session, FIXTURES / "python_app", "pytest-orch-failure")
        try:
            result = run_scanners(
                audit.id,
                FIXTURES / "python_app",
                configs=ScannerConfigs(
                    gitleaks=GitleaksConfig(image=MISSING_IMAGE, timeout_seconds=60)
                ),
                output_root=tmp_path / "out",
            )

            gitleaks = _outcome(result, ScannerTool.GITLEAKS)
            assert gitleaks.status is ScannerRunStatus.FAILED
            assert gitleaks.error and "exited with" in gitleaks.error
            assert gitleaks.stored == 0

            for tool in (ScannerTool.SEMGREP, ScannerTool.OSV, ScannerTool.SCANCODE):
                outcome = _outcome(result, tool)
                assert outcome.ok, (tool, outcome.error)
                assert outcome.stored > 0

            runs = {
                run.tool: run
                for run in session.execute(
                    select(ScannerRun).where(ScannerRun.audit_id == audit.id)
                ).scalars()
            }
            assert runs[ScannerTool.GITLEAKS].status is ScannerRunStatus.FAILED
            assert "exited with" in runs[ScannerTool.GITLEAKS].error
            for tool in (ScannerTool.SEMGREP, ScannerTool.OSV, ScannerTool.SCANCODE):
                assert runs[tool].status is ScannerRunStatus.SUCCEEDED

            # The other scanners' findings survived the failure.
            categories = set(
                session.execute(
                    select(Finding.category).where(Finding.audit_id == audit.id)
                ).scalars()
            )
            assert {"security", "dependency", "license"} <= categories
            assert "secret" not in categories
        finally:
            _cleanup(session, repository, audit)
