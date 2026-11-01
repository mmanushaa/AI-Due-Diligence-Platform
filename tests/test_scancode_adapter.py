"""Step 1.7: the ScanCode license adapter against the benchmark fixtures.

The docker-backed tests are skipped when the scanner image is missing (see README).
The registry lookup in the restrictive-license tests needs network; the
missing-data test passes either way, because a failed lookup is also missing data.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.db.enums import AuditStatus, SourceType
from app.db.models import Audit, Evidence, Finding, Repository, ScannerRun
from app.db.session import get_session_factory
from app.scanners import scancode
from app.scanners.scancode import (
    SCANCODE_IMAGE,
    DeclaredLicense,
    Dependency,
    ParsedReport,
    ScancodeConfig,
    classify_license,
    load_policy,
    parse_report,
    scan_audit,
)

FIXTURES = Path(__file__).resolve().parents[1] / "benchmark" / "repos"
DOCKER = shutil.which("docker")
MISSING_IMAGE = "due-diligence-scancode:does-not-exist"


@pytest.fixture(scope="module", autouse=True)
def _drop_stale_repositories():
    """A killed run can leave rows behind; they would skew other modules' counts."""
    with get_session_factory()() as session:
        session.execute(delete(Repository).where(Repository.name.like("pytest-scancode-%")))
        session.commit()
    yield


def _image_present(image: str) -> bool:
    if DOCKER is None:
        return False
    return subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, check=False
    ).returncode == 0


requires_scanner = pytest.mark.skipif(
    not _image_present(SCANCODE_IMAGE), reason=f"docker image {SCANCODE_IMAGE} is not available"
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


def _finding(session, audit_id, *, title_contains=None, source_tool=None, verdict=None):
    statements = [Finding.audit_id == audit_id]
    if title_contains is not None:
        statements.append(Finding.title.contains(title_contains))
    if source_tool is not None:
        statements.append(Finding.source_tool == source_tool)
    if verdict is not None:
        statements.append(Finding.verdict == verdict)
    return session.execute(select(Finding).where(*statements)).scalars().all()


# --- policy (no docker) -----------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "restrictive", "family", "severity", "covered"),
    [
        ("GPL-2.0-only", True, "GPL", "medium", True),
        ("GPL-2.0-or-later", True, "GPL", "medium", True),
        ("GPL-3.0-only", True, "GPL", "medium", True),
        ("gpl-2.0-plus", True, "GPL", "medium", True),
        ("GNU General Public License v2 (GPLv2)", True, "GPL", "medium", True),
        ("AGPL-3.0-or-later", True, "AGPL", "high", True),
        ("SSPL-1.0", True, "SSPL", "high", True),
        ("MIT OR GPL-2.0-only", True, "GPL", "medium", True),
        ("MIT", False, None, None, True),
        ("MIT AND Apache-2.0", False, None, None, True),
        ("BSD-3-Clause", False, None, None, True),
        ("Apache-2.0 WITH LLVM-exception", False, None, None, True),
        ("ISC", False, None, None, True),
        # Weak copyleft and unknown licenses are in neither list, so they are not
        # covered: that becomes insufficient_evidence, never "permissive".
        ("LGPL-2.1-only", False, None, None, False),
        ("MPL-2.0", False, None, None, False),
        ("Proprietary Custom License", False, None, None, False),
    ],
)
def test_license_classification(expression, restrictive, family, severity, covered):
    result = classify_license(expression, load_policy())
    assert result.restrictive is restrictive
    assert result.family == family
    assert (result.severity.value if result.severity else None) == severity
    assert result.covered is covered


def test_policy_lists_restrictive_and_permissive_families():
    policy = load_policy()
    restrictive = {family.name for family in policy.restrictive}
    permissive = {family.name for family in policy.permissive}

    assert {"GPL", "AGPL", "SSPL"} <= restrictive
    assert {"MIT", "Apache-2.0", "BSD"} <= permissive
    assert not restrictive & permissive
    assert {family.name: family.severity.value for family in policy.restrictive}["GPL"] == "medium"


# --- report parsing (no docker) ---------------------------------------------


def _report_payload() -> dict:
    return {
        "headers": [{"tool_name": "scancode-toolkit", "tool_version": "32.5.0", "errors": []}],
        "packages": [
            {
                "purl": "pkg:npm/js-app-fixture@0.1.0",
                "declared_license_expression": "mit",
            }
        ],
        "dependencies": [
            {
                "purl": "pkg:npm/%40ckeditor/ckeditor5-core",
                "extracted_requirement": "41.4.2",
                "datafile_path": "repo/package.json",
            },
            {
                "purl": "pkg:npm/lodash",
                "extracted_requirement": "^4.17.0",
                "datafile_path": "repo/package.json",
            },
            {
                "purl": "pkg:pypi/mysqlclient@2.2.0",
                "extracted_requirement": "mysqlclient==2.2.0",
                "datafile_path": "repo/requirements.txt",
            },
        ],
        "files": [
            {
                "path": "repo/package.json",
                "scan_errors": [],
                "package_data": [
                    {
                        "type": "npm",
                        "name": "js-app-fixture",
                        "dependencies": [{"purl": "pkg:npm/lodash"}],
                    }
                ],
            },
            {
                "path": "repo/a.py",
                "scan_errors": [],
                "license_detections": [
                    {
                        "license_expression": "gpl-3.0",
                        "license_expression_spdx": "GPL-3.0-only",
                        "matches": [
                            {
                                "start_line": 1,
                                "end_line": 1,
                                "rule_identifier": "gpl-3.0_623.RULE",
                                "matcher": "2-aho",
                                "score": 100.0,
                            }
                        ],
                    }
                ],
            },
            {"path": "repo/broken.bin", "scan_errors": ["Unknown file format"], "package_data": []},
        ],
    }


def test_parse_report_normalizes_paths_versions_and_gaps():
    report = parse_report(_report_payload())

    by_name = {dependency.name: dependency for dependency in report.dependencies}
    assert set(by_name) == {"@ckeditor/ckeditor5-core", "lodash", "mysqlclient"}
    # The npm purl carries no version; the exact pin in the manifest is used.
    ckeditor = by_name["@ckeditor/ckeditor5-core"]
    assert (ckeditor.version, ckeditor.pinned) == ("41.4.2", True)
    assert ckeditor.manifest_path == "package.json"
    # A range names no installed version, so it cannot be verified.
    assert (by_name["lodash"].version, by_name["lodash"].pinned) == ("", False)
    assert by_name["mysqlclient"].ecosystem == "pypi"

    assert [hit.path for hit in report.files] == ["a.py"]
    assert report.files[0].spdx == "GPL-3.0-only"
    assert (report.files[0].line_start, report.files[0].line_end) == (1, 1)
    assert report.unreadable_files == ("broken.bin",)
    assert report.header_errors == ()
    assert report.version == "32.5.0"


def test_parse_report_surfaces_a_header_error():
    payload = _report_payload()
    payload["headers"][0]["errors"] = ["Failed to scan some files"]
    assert parse_report(payload).header_errors == ("Failed to scan some files",)


# --- planning (no docker) ---------------------------------------------------


def test_missing_license_data_is_insufficient_evidence(tmp_path, monkeypatch):
    """A dependency with no license anywhere still produces a finding."""
    monkeypatch.setattr(
        scancode,
        "lookup_declared_license",
        lambda *args, **kwargs: DeclaredLicense(None, "pypi", "PyPI returned HTTP 404"),
    )
    (tmp_path / "requirements.txt").write_text(
        "internal-billing-sdk==0.4.1\n", encoding="utf-8"
    )
    report = ParsedReport(
        files=(),
        dependencies=(
            Dependency(
                ecosystem="pypi",
                name="internal-billing-sdk",
                version="0.4.1",
                manifest_path="requirements.txt",
                requirement="internal-billing-sdk==0.4.1",
                pinned=True,
            ),
        ),
        unreadable_files=(),
        header_errors=(),
        version="32.5.0",
    )

    candidates = scancode._plan(report, tmp_path, load_policy(), ScancodeConfig())

    assert len(candidates) == 1
    finding = candidates[0]
    assert finding.verdict.value == "insufficient_evidence"
    assert finding.verification.value == "unresolved"
    assert "No license data" in finding.title
    assert "PyPI returned HTTP 404" in finding.reasoning
    assert (finding.file_path, finding.line_start, finding.line_end) == ("requirements.txt", 1, 1)


def test_registry_lookup_disabled_is_insufficient_evidence(tmp_path):
    """With the lookup switched off, dependencies are unverified, not compliant."""
    (tmp_path / "requirements.txt").write_text("mysqlclient==2.2.0\n", encoding="utf-8")
    report = ParsedReport(
        files=(),
        dependencies=(
            Dependency(
                ecosystem="pypi",
                name="mysqlclient",
                version="2.2.0",
                manifest_path="requirements.txt",
                requirement="mysqlclient==2.2.0",
                pinned=True,
            ),
        ),
        unreadable_files=(),
        header_errors=(),
        version="32.5.0",
    )

    candidates = scancode._plan(
        report, tmp_path, load_policy(), ScancodeConfig(allow_network=False)
    )

    assert len(candidates) == 1
    assert candidates[0].verdict.value == "insufficient_evidence"
    assert "allow_network" in candidates[0].reasoning


def test_unpinned_dependencies_are_reported_once_per_manifest(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"express": "^4.21.0", "lodash": "^4.17.0"}}),
        encoding="utf-8",
    )
    report = ParsedReport(
        files=(),
        dependencies=tuple(
            Dependency(
                ecosystem="npm",
                name=name,
                version="",
                manifest_path="package.json",
                requirement=requirement,
                pinned=False,
            )
            for name, requirement in (("express", "^4.21.0"), ("lodash", "^4.17.0"))
        ),
        unreadable_files=(),
        header_errors=(),
        version="32.5.0",
    )

    candidates = scancode._plan(report, tmp_path, load_policy(), ScancodeConfig())

    assert len(candidates) == 1
    assert candidates[0].verdict.value == "insufficient_evidence"
    assert "2 dependencies in package.json are not pinned" in candidates[0].title
    assert "express ^4.21.0" in candidates[0].reasoning


def test_incomplete_scan_output_is_insufficient_evidence(tmp_path):
    report = ParsedReport(
        files=(),
        dependencies=(),
        unreadable_files=("broken.bin", "vendor/blob.bin"),
        header_errors=(),
        version="32.5.0",
    )

    candidates = scancode._plan(report, tmp_path, load_policy(), ScancodeConfig())

    assert len(candidates) == 1
    assert candidates[0].verdict.value == "insufficient_evidence"
    assert "License inventory is incomplete" in candidates[0].title
    assert candidates[0].file_path == "broken.bin"


def test_restrictive_license_in_a_file_cites_the_file(tmp_path):
    (tmp_path / "COPYING").write_text(
        "GNU AFFERO GENERAL PUBLIC LICENSE\nVersion 3, 19 November 2007\n", encoding="utf-8"
    )
    report = ParsedReport(
        files=(
            scancode.FileLicense(
                path="COPYING",
                expression="agpl-3.0",
                spdx="AGPL-3.0-only",
                line_start=1,
                line_end=2,
                rule_id="agpl-3.0_1.RULE",
                matcher="2-aho",
                score=99.5,
            ),
        ),
        dependencies=(),
        unreadable_files=(),
        header_errors=(),
        version="32.5.0",
    )

    candidates = scancode._plan(report, tmp_path, load_policy(), ScancodeConfig())

    assert len(candidates) == 1
    finding = candidates[0]
    assert finding.verdict.value == "issue"
    assert finding.severity.value == "high"  # AGPL is high in the policy
    assert finding.rule_id == "scancode:AGPL-3.0-only"
    assert (finding.file_path, finding.line_start, finding.line_end) == ("COPYING", 1, 2)
    assert "AGPL" in finding.reasoning


def test_license_outside_the_policy_is_not_called_permissive(tmp_path):
    report = ParsedReport(
        files=(
            scancode.FileLicense(
                path="LICENSE.txt",
                expression="proprietary-license",
                spdx=None,
                line_start=1,
                line_end=1,
                rule_id=None,
                matcher=None,
                score=None,
            ),
        ),
        dependencies=(),
        unreadable_files=(),
        header_errors=(),
        version="32.5.0",
    )

    candidates = scancode._plan(report, tmp_path, load_policy(), ScancodeConfig())

    assert len(candidates) == 1
    assert candidates[0].verdict.value == "insufficient_evidence"
    assert "not covered by the policy" in candidates[0].title


# --- fixtures through the scanner image -------------------------------------


@requires_scanner
def test_python_restrictive_dependency_is_detected(tmp_path):
    with get_session_factory()() as session:
        repository, audit = _audit_for(
            session, FIXTURES / "python_app", "pytest-scancode-py"
        )
        try:
            summary = scan_audit(
                session, audit.id, FIXTURES / "python_app", output_root=tmp_path / "out"
            )

            assert summary.status.value == "succeeded"
            findings = _finding(session, audit.id, source_tool="license-policy:GPL")
            assert len(findings) == 1
            finding = findings[0]
            assert finding.category.value == "license"
            assert finding.origin.value == "scanner"
            assert finding.verification_status.value == "scanner_confirmed"
            assert finding.severity.value == "medium"
            assert finding.verdict.value == "issue"
            assert finding.title.startswith("mysqlclient 2.2.0 is")
            assert "GPL" in finding.reasoning
            assert "pypi" in finding.reasoning

            evidence = session.execute(
                select(Evidence).where(Evidence.finding_id == finding.id)
            ).scalar_one()
            assert evidence.file_path == "requirements.txt"
            assert (evidence.line_start, evidence.line_end) == (4, 4)
            assert evidence.snippet == "mysqlclient==2.2.0"
            assert evidence.kind.value == "primary"

            # Every other dependency in the fixture is permissive, so nothing else
            # is reported and nothing is claimed compliant that we could not check.
            assert _finding(session, audit.id, verdict="insufficient_evidence") == []

            run = session.get(ScannerRun, summary.scanner_run_id)
            assert run.tool.value == "scancode"
            assert run.status.value == "succeeded"
            assert run.version == "32.5.0"
            assert run.error is None
            assert run.raw_output_path and Path(run.raw_output_path).is_file()
            assert run.started_at is not None and run.finished_at is not None
        finally:
            _cleanup(session, repository, audit)


@requires_scanner
def test_js_restrictive_dependency_is_detected(tmp_path):
    with get_session_factory()() as session:
        repository, audit = _audit_for(session, FIXTURES / "js_app", "pytest-scancode-js")
        try:
            summary = scan_audit(
                session, audit.id, FIXTURES / "js_app", output_root=tmp_path / "out"
            )
            assert summary.status.value == "succeeded"

            findings = _finding(session, audit.id)
            assert len(findings) == 1
            finding = findings[0]
            assert finding.title.startswith("@ckeditor/ckeditor5-core 41.4.2 is GPL-2.0-or-later")
            assert finding.severity.value == "medium"
            assert finding.verdict.value == "issue"
            assert "npm" in finding.reasoning

            evidence = session.execute(
                select(Evidence).where(Evidence.finding_id == finding.id)
            ).scalar_one()
            assert evidence.file_path == "package.json"
            assert (evidence.line_start, evidence.line_end) == (13, 13)
            assert "@ckeditor/ckeditor5-core" in evidence.snippet
        finally:
            _cleanup(session, repository, audit)


@requires_scanner
def test_missing_dependency_license_is_stored_with_insufficient_evidence(tmp_path):
    """The end-to-end version of the missing-data case: the registry has nothing."""
    snapshot = tmp_path / "python_app"
    shutil.copytree(FIXTURES / "python_app", snapshot)
    with (snapshot / "requirements.txt").open("a", encoding="utf-8") as handle:
        handle.write("internal-billing-sdk==0.4.1\n")

    with get_session_factory()() as session:
        repository, audit = _audit_for(session, snapshot, "pytest-scancode-missing")
        try:
            summary = scan_audit(session, audit.id, snapshot, output_root=tmp_path / "out")
            assert summary.status.value == "succeeded"

            findings = _finding(
                session, audit.id, title_contains="No license data for internal-billing-sdk"
            )
            assert len(findings) == 1
            finding = findings[0]
            assert finding.category.value == "license"
            assert finding.origin.value == "scanner"
            assert finding.verification_status.value == "unresolved"
            assert finding.verdict.value == "insufficient_evidence"
            assert finding.confidence is None

            evidence = session.execute(
                select(Evidence).where(Evidence.finding_id == finding.id)
            ).scalar_one()
            assert evidence.file_path == "requirements.txt"
            assert (evidence.line_start, evidence.line_end) == (5, 5)
            assert evidence.snippet == "internal-billing-sdk==0.4.1"
        finally:
            _cleanup(session, repository, audit)


@requires_scanner
def test_license_text_is_detected_with_file_evidence(tmp_path):
    snapshot = tmp_path / "licensed_app"
    snapshot.mkdir()
    (snapshot / "module.py").write_text(
        "# SPDX-License-Identifier: GPL-3.0-only\nprint('x')\n", encoding="utf-8"
    )

    with get_session_factory()() as session:
        repository, audit = _audit_for(session, snapshot, "pytest-scancode-file")
        try:
            summary = scan_audit(session, audit.id, snapshot, output_root=tmp_path / "out")
            assert summary.status.value == "succeeded"

            findings = _finding(session, audit.id, source_tool="scancode:GPL-3.0-only")
            assert len(findings) == 1
            finding = findings[0]
            assert finding.category.value == "license"
            assert finding.verdict.value == "issue"
            assert finding.severity.value == "medium"

            evidence = session.execute(
                select(Evidence).where(Evidence.finding_id == finding.id)
            ).scalar_one()
            assert evidence.file_path == "module.py"
            assert (evidence.line_start, evidence.line_end) == (1, 1)
            assert "SPDX-License-Identifier" in evidence.snippet
        finally:
            _cleanup(session, repository, audit)


@requires_scanner
def test_rescanning_does_not_duplicate_findings(tmp_path):
    with get_session_factory()() as session:
        repository, audit = _audit_for(session, FIXTURES / "js_app", "pytest-scancode-twice")
        try:
            first = scan_audit(
                session, audit.id, FIXTURES / "js_app", output_root=tmp_path / "out"
            )
            second = scan_audit(
                session, audit.id, FIXTURES / "js_app", output_root=tmp_path / "out"
            )

            assert first.stored == 1
            assert second.detected == 1 and second.stored == 0
            assert len(_finding(session, audit.id)) == 1
        finally:
            _cleanup(session, repository, audit)


@pytest.mark.skipif(DOCKER is None, reason="docker is not installed")
def test_scanner_failure_is_recorded(tmp_path):
    with get_session_factory()() as session:
        repository, audit = _audit_for(session, FIXTURES / "python_app", "pytest-scancode-fail")
        try:
            summary = scan_audit(
                session,
                audit.id,
                FIXTURES / "python_app",
                config=ScancodeConfig(image=MISSING_IMAGE, timeout_seconds=60),
                output_root=tmp_path / "out",
            )

            assert summary.status.value == "failed"
            assert summary.stored == 0
            assert summary.error

            run = session.get(ScannerRun, summary.scanner_run_id)
            assert run.tool.value == "scancode"
            assert run.status.value == "failed"
            assert run.error and "exited with" in run.error
            assert run.finished_at is not None
        finally:
            _cleanup(session, repository, audit)
