"""Step 1.2: POST /api/v1/repositories and safe zip extraction.

Every archive below is crafted in memory; nothing in it is executed.
"""

import hashlib
import io
import os
import stat
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.core.config import settings
from app.db.models import Repository
from app.db.session import get_session_factory
from app.main import app

UPLOAD_URL = "/api/v1/repositories"


def zip_bytes(entries: dict[str, bytes], compress: bool = True) -> bytes:
    buffer = io.BytesIO()
    method = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(buffer, "w", method) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def zip_with_symlink(link_name: str, target: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo(link_name)
        info.create_system = 3  # unix, so the mode bits are honoured
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, target)
    return buffer.getvalue()


def zip_with_special_file(name: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("keep.txt", b"hello")
        info = zipfile.ZipInfo(name)
        info.create_system = 3
        info.external_attr = (stat.S_IFIFO | 0o644) << 16  # fifo, not a regular file
        archive.writestr(info, b"")
    return buffer.getvalue()


def post_zip(client: TestClient, data: bytes, name: str = "pytest-upload"):
    return client.post(
        UPLOAD_URL,
        files={"file": ("repo.zip", data, "application/zip")},
        data={"name": name},
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean_repositories():
    yield
    with get_session_factory()() as session:
        session.execute(delete(Repository).where(Repository.name.like("pytest-%")))
        session.commit()


@pytest.fixture
def repository_count():
    def count() -> int:
        with get_session_factory()() as session:
            return session.query(Repository).filter(Repository.name.like("pytest-%")).count()

    return count


def test_normal_zip_is_accepted_and_recorded(client, repository_count):
    payload = zip_bytes(
        {
            "README.md": b"# demo\n",
            "src/app.py": b"print('hello')\n",
            "src/utils/__init__.py": b"",
        }
    )

    response = post_zip(client, payload)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["file_count"] == 3
    assert body["sha256"] == hashlib.sha256(payload).hexdigest()
    assert body["size_bytes"] == len(payload)
    assert body["source_type"] == "zip"
    assert body["uncompressed_bytes"] == sum(
        len(data) for data in (b"# demo\n", b"print('hello')\n", b"")
    )

    stored = os.path.join(body["storage_path"], "src", "app.py")
    with open(stored, "rb") as handle:
        assert handle.read() == b"print('hello')\n"

    assert stat.S_IMODE(os.stat(stored).st_mode) & 0o222 == 0, "snapshot must be read-only"
    assert stat.S_IMODE(os.stat(body["storage_path"]).st_mode) & 0o222 == 0

    assert repository_count() == 1


@pytest.mark.parametrize("entry", ["../evil.txt", "src/../../evil.txt", "..\\evil.txt"])
def test_path_traversal_is_rejected(client, tmp_path, entry):
    payload = zip_bytes({entry: b"escaped", "safe.txt": b"ok"})

    response = post_zip(client, payload)

    assert response.status_code == 400
    assert "path traversal" in response.json()["detail"]
    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path / "uploads" / "evil.txt").exists()


@pytest.mark.parametrize("entry", ["/etc/passwd", "C:/Windows/system.ini", "\\\\server\\share\\x"])
def test_absolute_paths_are_rejected(client, entry):
    response = post_zip(client, zip_bytes({entry: b"x"}))

    assert response.status_code == 400
    assert "absolute path" in response.json()["detail"]


def test_symlink_escape_is_rejected(client, tmp_path):
    payload = zip_with_symlink("link", "../../etc/passwd")

    response = post_zip(client, payload)

    assert response.status_code == 400
    assert "symbolic link" in response.json()["detail"]
    assert not (tmp_path / "uploads" / "link").exists()


def test_special_files_are_ignored(client):
    response = post_zip(client, zip_with_special_file("pipe"))

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["file_count"] == 1
    assert not os.path.exists(os.path.join(body["storage_path"], "pipe"))


def test_too_many_files_is_rejected(client, monkeypatch, repository_count):
    monkeypatch.setattr(settings, "max_file_count", 3)
    payload = zip_bytes({f"file{i}.txt": b"x" for i in range(4)})

    response = post_zip(client, payload)

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "too many files" in detail and "limit of 3" in detail
    assert repository_count() == 0


def test_oversized_uncompressed_content_is_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "max_uncompressed_bytes", 1_000_000)
    monkeypatch.setattr(settings, "max_compression_ratio", 10_000)
    payload = zip_bytes({"big.bin": b"\0" * 2_000_000})

    response = post_zip(client, payload)

    assert response.status_code == 400
    assert "above the limit" in response.json()["detail"]


def test_zip_bomb_compression_ratio_is_rejected(client, monkeypatch, repository_count):
    monkeypatch.setattr(settings, "max_uncompressed_bytes", 100_000_000)
    monkeypatch.setattr(settings, "max_compression_ratio", 50)
    payload = zip_bytes({"bomb.bin": b"\0" * 5_000_000})
    assert len(payload) < 100_000, "the archive itself is tiny, which is the point"

    response = post_zip(client, payload)

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "compression ratio" in detail and "50:1" in detail
    assert repository_count() == 0


def test_upload_over_size_limit_is_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_bytes", 200)
    payload = zip_bytes({"a.txt": b"x" * 2000}, compress=False)

    response = post_zip(client, payload)

    assert response.status_code == 413
    assert "upload limit" in response.json()["detail"]


def test_non_zip_upload_is_rejected(client):
    response = client.post(
        UPLOAD_URL,
        files={"file": ("repo.zip", b"this is not a zip", "application/zip")},
        data={"name": "pytest-notzip"},
    )

    assert response.status_code == 400
    assert "not a valid zip" in response.json()["detail"]
