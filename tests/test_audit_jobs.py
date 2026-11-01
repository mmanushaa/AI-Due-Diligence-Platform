"""Step 1.9: the audit API, the background run and its status bookkeeping.

The docker-backed tests are skipped when a scanner image is missing (see README).
No test needs a broker: enqueueing is stubbed, and the run itself is called directly.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.api import audits as audits_api
from app.db.enums import AuditStatus, ScannerRunStatus, ScannerTool, SourceType, Stage
from app.db.models import Audit, AuditLog, Finding, Repository, ScannerRun
from app.db.session import get_session_factory
from app.jobs.audit import AuditRunError, run_audit
from app.jobs.celery_app import celery_app
from app.main import app
from app.scanners.gitleaks import GITLEAKS_IMAGE, GitleaksConfig
from app.scanners.osv import OSV_IMAGE
from app.scanners.orchestrator import ScannerConfigs
from app.scanners.scancode import SCANCODE_IMAGE
from app.scanners.semgrep import SEMGREP_IMAGE

FIXTURES = Path(__file__).resolve().parents[1] / "benchmark" / "repos"
DOCKER = shutil.which("docker")
IMAGES = (GITLEAKS_IMAGE, SEMGREP_IMAGE, OSV_IMAGE, SCANCODE_IMAGE)
MISSING_IMAGE = "due-diligence-gitleaks:does-not-exist"


@pytest.fixture(scope="module", autouse=True)
def _drop_stale_repositories():
    with get_session_factory()() as session:
        session.execute(delete(Repository).where(Repository.name.like("pytest-audit-%")))
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
    reason="every scanner image is required for a full audit run",
)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def repository():
    snapshot = FIXTURES / "python_app"
    with get_session_factory()() as session:
        repository = Repository(
            name=f"pytest-audit-{snapshot.name}",
            source_type=SourceType.ZIP,
            storage_path=str(snapshot),
        )
        session.add(repository)
        session.commit()
        session.refresh(repository)
        repository_id = repository.id
    yield repository_id
    with get_session_factory()() as session:
        session.execute(delete(Repository).where(Repository.id == repository_id))
        session.commit()


def _audit_row(audit_id) -> Audit:
    with get_session_factory()() as session:
        audit = session.get(Audit, audit_id)
        assert audit is not None
        return audit


class _Recorder:
    """Stands in for the Celery task so no broker is needed."""

    def __init__(self, *, raises: Exception | None = None):
        self.calls: list[str] = []
        self.raises = raises

    def delay(self, audit_id: str):
        self.calls.append(audit_id)
        if self.raises is not None:
            raise self.raises
        return None


# --- API --------------------------------------------------------------------


def test_starting_an_audit_queues_it_and_enqueues_the_task(client, repository, monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(audits_api, "run_audit_task", recorder)

    response = client.post(f"/api/v1/repositories/{repository}/audits")
    assert response.status_code == 202

    body = response.json()
    assert body["repository_id"] == str(repository)
    assert body["status"] == "queued"
    assert body["current_stage"] is None
    assert body["started_at"] is None
    assert body["finished_at"] is None
    assert body["error"] is None
    assert recorder.calls == [body["id"]]

    audit = _audit_row(body["id"])
    assert audit.status is AuditStatus.QUEUED
    assert audit.current_stage is None

    read = client.get(f"/api/v1/audits/{body['id']}")
    assert read.status_code == 200
    assert read.json() == body


def test_starting_an_audit_for_an_unknown_repository_is_404(client, monkeypatch):
    monkeypatch.setattr(audits_api, "run_audit_task", _Recorder())
    response = client.post("/api/v1/repositories/00000000-0000-0000-0000-0000000000ff/audits")
    assert response.status_code == 404


def test_unknown_audit_is_404(client):
    response = client.get("/api/v1/audits/00000000-0000-0000-0000-0000000000ff")
    assert response.status_code == 404


def test_broker_failure_marks_the_audit_failed(client, repository, monkeypatch):
    monkeypatch.setattr(
        audits_api, "run_audit_task", _Recorder(raises=ConnectionError("redis is down"))
    )

    response = client.post(f"/api/v1/repositories/{repository}/audits")
    assert response.status_code == 503
    assert "queue is unavailable" in response.json()["detail"]

    with get_session_factory()() as session:
        audit = session.execute(
            select(Audit).where(Audit.repository_id == repository)
        ).scalars().one()
        assert audit.status is AuditStatus.FAILED
        assert audit.finished_at is not None

        log = session.execute(
            select(AuditLog).where(AuditLog.object_id == str(audit.id))
        ).scalars().one()
        assert log.action == "audit_failed"
        assert "could not enqueue" in log.after["reason"]

    read = client.get(f"/api/v1/audits/{audit.id}")
    assert read.json()["status"] == "failed"
    assert "could not enqueue" in read.json()["error"]


# --- task configuration ------------------------------------------------------


def test_the_task_acknowledges_late():
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert celery_app.conf.task_default_queue == "audits"


# --- the run itself ----------------------------------------------------------


def test_a_missing_snapshot_fails_the_audit_with_the_reason(tmp_path):
    with get_session_factory()() as session:
        repository = Repository(
            name="pytest-audit-missing",
            source_type=SourceType.ZIP,
            storage_path=str(tmp_path / "gone"),
        )
        session.add(repository)
        session.commit()
        session.refresh(repository)
        audit = Audit(repository_id=repository.id, status=AuditStatus.QUEUED)
        session.add(audit)
        session.commit()
        session.refresh(audit)
        audit_id = audit.id
        repository_id = repository.id

    try:
        with pytest.raises(AuditRunError):
            run_audit(audit_id)

        audit = _audit_row(audit_id)
        assert audit.status is AuditStatus.FAILED
        assert audit.current_stage is Stage.INTAKE
        assert audit.started_at is not None and audit.finished_at is not None

        with get_session_factory()() as session:
            log = session.execute(
                select(AuditLog).where(AuditLog.object_id == str(audit_id))
            ).scalars().one()
            assert log.action == "audit_failed"
            assert "snapshot" in log.after["reason"]

        with TestClient(app) as test_client:
            body = test_client.get(f"/api/v1/audits/{audit_id}").json()
        assert body["status"] == "failed"
        assert body["current_stage"] == "intake"
        assert "does not exist" in body["error"]
    finally:
        with get_session_factory()() as session:
            session.execute(delete(Repository).where(Repository.id == repository_id))
            session.commit()


@requires_scanners
def test_a_full_run_moves_queued_to_running_to_completed(tmp_path):
    snapshot = FIXTURES / "python_app"
    with get_session_factory()() as session:
        repository = Repository(
            name="pytest-audit-full", source_type=SourceType.ZIP, storage_path=str(snapshot)
        )
        session.add(repository)
        session.commit()
        session.refresh(repository)
        audit = Audit(repository_id=repository.id, status=AuditStatus.QUEUED)
        session.add(audit)
        session.commit()
        session.refresh(audit)
        audit_id = audit.id
        repository_id = repository.id

    try:
        assert _audit_row(audit_id).status is AuditStatus.QUEUED

        result = run_audit(audit_id, output_root=tmp_path / "out")

        assert result.status is AuditStatus.COMPLETED
        assert result.stage is Stage.SCANNERS
        assert result.files.included > 0
        assert result.scanners.all_succeeded, [
            (o.tool.value, o.error) for o in result.scanners.failed
        ]

        audit = _audit_row(audit_id)
        assert audit.status is AuditStatus.COMPLETED
        assert audit.current_stage is Stage.SCANNERS
        assert audit.started_at is not None and audit.finished_at is not None

        with get_session_factory()() as session:
            categories = set(
                session.execute(
                    select(Finding.category).where(Finding.audit_id == audit_id)
                ).scalars()
            )
            assert {"secret", "security", "dependency", "license"} <= categories

            runs = session.execute(
                select(ScannerRun).where(ScannerRun.audit_id == audit_id)
            ).scalars().all()
            assert {run.tool for run in runs} == {
                ScannerTool.GITLEAKS,
                ScannerTool.SEMGREP,
                ScannerTool.OSV,
                ScannerTool.SCANCODE,
            }
            assert all(run.status is ScannerRunStatus.SUCCEEDED for run in runs)
    finally:
        with get_session_factory()() as session:
            session.execute(delete(Repository).where(Repository.id == repository_id))
            session.commit()


@requires_scanners
def test_one_failed_scanner_does_not_fail_the_audit(tmp_path):
    snapshot = FIXTURES / "python_app"
    with get_session_factory()() as session:
        repository = Repository(
            name="pytest-audit-degraded", source_type=SourceType.ZIP, storage_path=str(snapshot)
        )
        session.add(repository)
        session.commit()
        session.refresh(repository)
        audit = Audit(repository_id=repository.id, status=AuditStatus.QUEUED)
        session.add(audit)
        session.commit()
        session.refresh(audit)
        audit_id = audit.id
        repository_id = repository.id

    try:
        result = run_audit(
            audit_id,
            configs=ScannerConfigs(
                gitleaks=GitleaksConfig(image=MISSING_IMAGE, timeout_seconds=60)
            ),
            output_root=tmp_path / "out",
        )

        assert result.status is AuditStatus.COMPLETED
        assert [outcome.tool for outcome in result.scanners.failed] == [ScannerTool.GITLEAKS]

        with get_session_factory()() as session:
            runs = {
                run.tool: run
                for run in session.execute(
                    select(ScannerRun).where(ScannerRun.audit_id == audit_id)
                ).scalars()
            }
            assert runs[ScannerTool.GITLEAKS].status is ScannerRunStatus.FAILED
            assert runs[ScannerTool.SEMGREP].status is ScannerRunStatus.SUCCEEDED
            categories = set(
                session.execute(
                    select(Finding.category).where(Finding.audit_id == audit_id)
                ).scalars()
            )
            assert {"security", "dependency", "license"} <= categories
    finally:
        with get_session_factory()() as session:
            session.execute(delete(Repository).where(Repository.id == repository_id))
            session.commit()
