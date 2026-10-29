"""Step 1.3: file filtering, language detection and the files table."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.enums import AuditStatus, SourceType
from app.db.models import Audit, File as FileModel, Repository
from app.db.session import get_session_factory
from app.intake.filter_config import FilterConfig
from app.intake.filtering import index_snapshot, scan_snapshot

FIXTURES = Path(__file__).resolve().parents[1] / "benchmark" / "repos"


def build_tree(root: Path, files: dict[str, bytes]) -> Path:
    for relative, payload in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return root


@pytest.mark.parametrize(
    "repo_name,total,expected_included",
    [("python_app", 11, 11), ("js_app", 10, 10)],
)
def test_fixture_snapshots_are_fully_indexed(repo_name, total, expected_included):
    summary = scan_snapshot(FIXTURES / repo_name)

    assert len(summary.records) == total
    assert summary.included == expected_included
    assert summary.excluded == 0
    assert summary.excluded_files == []
    assert len({record.path for record in summary.records}) == total
    assert all(record.sha256 for record in summary.records)


@pytest.mark.parametrize(
    "repo_name,path,language",
    [
        ("python_app", "app/database.py", "python"),
        ("python_app", "requirements.txt", "text"),
        ("python_app", "app/routes/orders.py", "python"),
        ("js_app", "server.js", "javascript"),
        ("js_app", "package.json", "json"),
        ("js_app", "src/lib/worker.js", "javascript"),
    ],
)
def test_language_detection_on_fixtures(repo_name, path, language):
    summary = scan_snapshot(FIXTURES / repo_name)
    by_path = {record.path: record for record in summary.records}

    assert by_path[path].language == language
    assert by_path[path].is_excluded is False
    assert by_path[path].exclude_reason is None


def test_every_exclusion_rule(tmp_path):
    root = build_tree(
        tmp_path / "snapshot",
        {
            "src/app.py": b"print('keep me')\n",
            "node_modules/left-pad/index.js": b"module.exports = 1;\n",
            "vendor/nested/lib.go": b"package lib\n",
            ".git/config": b"[core]\n",
            "dist/bundle.js": b"console.log('built')\n",
            "build/lib/old.py": b"# generated\n",
            "__pycache__/mod.cpython-312.pyc": b"\x00\x01\x02",
            "assets/logo.png": b"\x89PNG\r\n\x1a\n fake image",
            "data/blob.dat": b"text then \x00 a null byte",
            "docs/huge.txt": b"a" * 2048,
        },
    )

    config = FilterConfig(max_file_bytes=1024)
    summary = scan_snapshot(root, config=config)
    reasons = {record.path: record.exclude_reason for record in summary.records}

    assert reasons["src/app.py"] is None
    assert reasons["node_modules/left-pad/index.js"] == "vendored_directory"
    assert reasons["vendor/nested/lib.go"] == "vendored_directory"
    assert reasons[".git/config"] == "vendored_directory"
    assert reasons["dist/bundle.js"] == "build_output"
    assert reasons["build/lib/old.py"] == "build_output"
    assert reasons["__pycache__/mod.cpython-312.pyc"] == "build_output"
    assert reasons["assets/logo.png"] == "binary_extension"
    assert reasons["data/blob.dat"] == "binary_null_byte"
    assert reasons["docs/huge.txt"] == "file_too_large"

    assert summary.included == 1
    assert summary.excluded == 9
    assert len(summary.records) == 10
    assert [record.path for record in summary.excluded_files] == sorted(
        path for path in reasons if path != "src/app.py"
    )


def test_config_controls_the_skip_lists(tmp_path):
    root = build_tree(
        tmp_path / "snapshot",
        {"src/app.py": b"x = 1\n", "generated/client.py": b"y = 2\n"},
    )

    default_scan = scan_snapshot(root)
    assert default_scan.included == 2

    custom = FilterConfig(vendored_directories=frozenset({"generated"}))
    custom_scan = scan_snapshot(root, config=custom)
    assert custom_scan.included == 1
    assert custom_scan.excluded_files[0].path == "generated/client.py"
    assert custom_scan.excluded_files[0].exclude_reason == "vendored_directory"


def _create_repository_and_audit(session, storage_path: str) -> tuple[Repository, Audit]:
    repository = Repository(
        name="pytest-filter",
        source_type=SourceType.ZIP,
        storage_path=storage_path,
    )
    session.add(repository)
    session.commit()
    session.refresh(repository)

    audit = Audit(repository_id=repository.id, status=AuditStatus.QUEUED)
    session.add(audit)
    session.commit()
    session.refresh(audit)
    return repository, audit


def test_files_are_stored_with_exclusion_state():
    root = build_tree(
        Path(".").resolve() / "data" / f"pytest-filter-{uuid.uuid4().hex}",
        {
            "src/app.py": b"print('hello')\n",
            "node_modules/pkg/index.js": b"module.exports = 1;\n",
            "assets/logo.png": b"\x89PNG\r\n\x1a\n fake",
        },
    )

    with get_session_factory()() as session:
        repository, audit = _create_repository_and_audit(session, str(root))
        try:
            summary = index_snapshot(session, audit.id, root)

            rows = session.execute(
                select(FileModel).where(FileModel.audit_id == audit.id)
            ).scalars().all()
            assert len(rows) == 3
            assert summary.included == 1 and summary.excluded == 2

            by_path = {row.path: row for row in rows}
            assert by_path["src/app.py"].is_excluded is False
            assert by_path["src/app.py"].language == "python"
            assert by_path["src/app.py"].sha256 is not None
            assert by_path["node_modules/pkg/index.js"].is_excluded is True
            assert by_path["node_modules/pkg/index.js"].exclude_reason == "vendored_directory"
            assert by_path["node_modules/pkg/index.js"].sha256 is None
            assert by_path["assets/logo.png"].exclude_reason == "binary_extension"

            # Re-indexing the same audit replaces its rows instead of duplicating them.
            index_snapshot(session, audit.id, root)
            again = session.execute(
                select(FileModel).where(FileModel.audit_id == audit.id)
            ).scalars().all()
            assert len(again) == 3
        finally:
            session.delete(audit)
            session.commit()
            session.delete(repository)
            session.commit()
