"""Upload persistence and the repository intake service."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.core.config import Settings, settings
from app.db.enums import SourceType
from app.db.models import Repository
from app.intake.errors import IntakeError, UploadTooLarge
from app.intake.extraction import ExtractionResult, extract_zip, make_read_only, remove_tree

STREAM_CHUNK = 1024 * 1024


async def save_upload(upload: UploadFile, destination: Path, max_bytes: int) -> tuple[str, int]:
    """Stream the upload to disk, enforcing the size limit and hashing as it goes."""
    digest = hashlib.sha256()
    size = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as out:
        while chunk := await upload.read(STREAM_CHUNK):
            size += len(chunk)
            if size > max_bytes:
                raise UploadTooLarge(
                    f"archive is larger than the {max_bytes} byte upload limit"
                )
            digest.update(chunk)
            out.write(chunk)
    if size == 0:
        raise IntakeError("uploaded file is empty")
    return digest.hexdigest(), size


async def store_repository(
    *,
    upload: UploadFile,
    name: str | None,
    uploaded_by_id: uuid.UUID,
    session: Session,
    app_settings: Settings = settings,
) -> tuple[Repository, ExtractionResult]:
    """Store one uploaded zip as a read-only snapshot and record it in the database."""
    root = Path(app_settings.upload_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)

    repository_id = uuid.uuid4()
    staged_archive = root / f".upload-{repository_id}.zip"
    staged_dir = root / f".staging-{repository_id}"
    final_dir = root / str(repository_id)

    try:
        sha256, size = await save_upload(upload, staged_archive, app_settings.max_upload_bytes)
        result = extract_zip(
            staged_archive,
            staged_dir,
            max_file_count=app_settings.max_file_count,
            max_uncompressed_bytes=app_settings.max_uncompressed_bytes,
            max_compression_ratio=app_settings.max_compression_ratio,
        )
        staged_dir.rename(final_dir)
        make_read_only(final_dir)

        repository = Repository(
            id=repository_id,
            name=name or (upload.filename or "repository").removesuffix(".zip"),
            source_type=SourceType.ZIP,
            source_ref=upload.filename,
            storage_path=str(final_dir),
            sha256=sha256,
            size_bytes=size,
            uploaded_by=uploaded_by_id,
        )
        session.add(repository)
        session.commit()
        session.refresh(repository)
        return repository, result
    except Exception:
        remove_tree(staged_dir)
        raise
    finally:
        staged_archive.unlink(missing_ok=True)
