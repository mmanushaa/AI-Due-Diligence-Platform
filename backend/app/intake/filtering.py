"""Snapshot walking and file filtering (step 1.3).

Every file in the snapshot is classified and recorded; nothing is silently
dropped. The rules and lists live in filter_config.py.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.db.models import File as FileModel
from app.intake.filter_config import (
    DEFAULT_FILTER_CONFIG,
    REASON_BINARY_EXT,
    REASON_BINARY_NULL,
    REASON_BUILD,
    REASON_SYMLINK,
    REASON_TOO_LARGE,
    REASON_VENDORED,
    FilterConfig,
)

HASH_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class FileRecord:
    path: str  # relative to the snapshot root, posix separators
    size_bytes: int
    language: str | None
    is_excluded: bool
    exclude_reason: str | None
    sha256: str | None


@dataclass(frozen=True)
class ScanSummary:
    included: int
    excluded: int
    records: tuple[FileRecord, ...]

    @property
    def excluded_files(self) -> list[FileRecord]:
        return [record for record in self.records if record.is_excluded]


def detect_language(filename: str, config: FilterConfig = DEFAULT_FILTER_CONFIG) -> str | None:
    """Map a file name to a language, or None when unknown."""
    lowered = filename.lower()
    if lowered in config.language_by_filename:
        return config.language_by_filename[lowered]
    return config.language_by_extension.get(Path(lowered).suffix)


def _contains_null_byte(path: Path, sample_bytes: int) -> bool:
    try:
        with path.open("rb") as handle:
            return b"\x00" in handle.read(sample_bytes)
    except OSError:
        return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def classify_file(
    full_path: Path,
    relative_path: str,
    *,
    config: FilterConfig = DEFAULT_FILTER_CONFIG,
) -> FileRecord:
    """Decide whether one file is indexed, and why it is not."""
    size_bytes = 0
    try:
        size_bytes = full_path.lstat().st_size
    except OSError:
        pass

    language = detect_language(full_path.name, config)
    directories = relative_path.split("/")[:-1]
    reason: str | None = None

    if full_path.is_symlink():
        reason = REASON_SYMLINK
    elif any(part in config.vendored_directories for part in directories):
        reason = REASON_VENDORED
    elif any(part in config.build_directories for part in directories):
        reason = REASON_BUILD
    elif size_bytes > config.max_file_bytes:
        reason = REASON_TOO_LARGE
    elif full_path.suffix.lower() in config.binary_extensions:
        reason = REASON_BINARY_EXT
    elif _contains_null_byte(full_path, config.null_byte_sample_bytes):
        reason = REASON_BINARY_NULL

    is_excluded = reason is not None
    return FileRecord(
        path=relative_path,
        size_bytes=size_bytes,
        language=language,
        is_excluded=is_excluded,
        exclude_reason=reason,
        # Hashing excluded binaries would only slow the walk down.
        sha256=None if is_excluded else _sha256_file(full_path),
    )


def scan_snapshot(
    root: Path, *, config: FilterConfig = DEFAULT_FILTER_CONFIG
) -> ScanSummary:
    """Walk every file under `root` and classify it. Nothing is skipped silently."""
    root = root.resolve()
    records: list[FileRecord] = []

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for filename in sorted(filenames):
            full_path = Path(dirpath) / filename
            relative_path = full_path.relative_to(root).as_posix()
            records.append(classify_file(full_path, relative_path, config=config))

    included = sum(1 for record in records if not record.is_excluded)
    return ScanSummary(
        included=included,
        excluded=len(records) - included,
        records=tuple(records),
    )


def store_files(
    session: Session,
    audit_id: uuid.UUID,
    records: tuple[FileRecord, ...],
) -> int:
    """Replace the audit's file rows with this scan, then commit."""
    session.execute(delete(FileModel).where(FileModel.audit_id == audit_id))
    session.add_all(
        [
            FileModel(
                audit_id=audit_id,
                path=record.path,
                language=record.language,
                size_bytes=record.size_bytes,
                sha256=record.sha256,
                is_excluded=record.is_excluded,
                exclude_reason=record.exclude_reason,
            )
            for record in records
        ]
    )
    session.commit()
    return len(records)


def index_snapshot(
    session: Session,
    audit_id: uuid.UUID,
    snapshot_root: Path,
    *,
    config: FilterConfig = DEFAULT_FILTER_CONFIG,
) -> ScanSummary:
    """Scan a stored repository snapshot and persist one row per file."""
    summary = scan_snapshot(snapshot_root, config=config)
    store_files(session, audit_id, summary.records)
    return summary
