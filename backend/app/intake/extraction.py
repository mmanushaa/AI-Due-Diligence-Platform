"""Safe zip extraction for untrusted uploads.

Rules (step 1.2):
* archive size, total uncompressed size, compression ratio and file count limits
* absolute paths and any `..` component are rejected
* symlinks and hardlinks are rejected, special files (fifo, socket, device) are skipped
* every entry is written inside the destination directory; nothing is executed
"""

from __future__ import annotations

import ntpath
import shutil
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.intake.errors import IntakeError

READ_CHUNK = 1024 * 1024


@dataclass
class ExtractionResult:
    file_count: int
    total_bytes: int
    skipped_special: int = 0


def _validate_name(name: str) -> tuple[str, ...]:
    """Return path components, or raise if the entry could escape the destination."""
    if "\x00" in name:
        raise IntakeError("archive entry name contains a null byte")
    # A zip may use either separator; treat both as separators before checking.
    normalised = name.replace("\\", "/")
    if ntpath.splitdrive(normalised)[0] or normalised.startswith("/"):
        raise IntakeError(f"archive entry has an absolute path: {name!r}")
    parts = tuple(part for part in normalised.split("/") if part not in ("", "."))
    if any(part == ".." for part in parts):
        raise IntakeError(f"archive entry has path traversal: {name!r}")
    if not parts:
        raise IntakeError(f"archive entry has an empty path: {name!r}")
    return parts


def _entry_is_symlink(info: zipfile.ZipInfo) -> bool:
    """Unix mode lives in the high 16 bits of external_attr."""
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


def _entry_is_special(info: zipfile.ZipInfo) -> bool:
    """Anything that is neither a regular file nor a directory (fifo, socket, device).

    Many writers store only permission bits (e.g. 0o600) and no file-type bits;
    that is the conventional regular file and must not be treated as special.
    """
    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if file_type == 0:
        return False
    return file_type not in (stat.S_IFREG, stat.S_IFDIR)


def extract_zip(
    archive: Path,
    destination: Path,
    *,
    max_file_count: int,
    max_uncompressed_bytes: int,
    max_compression_ratio: int,
) -> ExtractionResult:
    """Extract `archive` into `destination`, enforcing every intake limit."""
    if not zipfile.is_zipfile(archive):
        raise IntakeError("uploaded file is not a valid zip archive")

    destination.mkdir(parents=True, exist_ok=True)
    destination_root = destination.resolve()

    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        if any(info.flag_bits & 0x1 for info in infos):
            raise IntakeError("archive contains encrypted entries, which are not supported")

        files = [info for info in infos if not info.is_dir()]
        if len(files) > max_file_count:
            raise IntakeError(
                f"archive has too many files: {len(files)} entries exceeds the limit of "
                f"{max_file_count}"
            )

        total_uncompressed = sum(info.file_size for info in files)
        total_compressed = sum(info.compress_size for info in files)
        if total_uncompressed > max_uncompressed_bytes:
            raise IntakeError(
                f"archive expands to {total_uncompressed} bytes, above the limit of "
                f"{max_uncompressed_bytes}"
            )
        if total_compressed > 0:
            ratio = total_uncompressed / total_compressed
            if ratio > max_compression_ratio:
                raise IntakeError(
                    f"archive compression ratio {ratio:.0f}:1 exceeds the limit of "
                    f"{max_compression_ratio}:1"
                )

        written = 0
        skipped = 0
        extracted_files = 0

        for info in infos:
            parts = _validate_name(info.filename)
            target = destination_root.joinpath(*parts)
            # Defence in depth: the resolved target must stay under the root.
            if not target.resolve().is_relative_to(destination_root):
                raise IntakeError(f"archive entry escapes the extraction directory: {info.filename!r}")

            if _entry_is_symlink(info):
                raise IntakeError(f"archive contains a symbolic link: {info.filename!r}")
            if _entry_is_special(info):
                # Hardlinks and device nodes have no safe meaning here: skip them.
                skipped += 1
                continue

            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as source, target.open("wb") as out:
                while chunk := source.read(READ_CHUNK):
                    written += len(chunk)
                    if written > max_uncompressed_bytes:
                        raise IntakeError(
                            "archive expands beyond the uncompressed size limit while extracting"
                        )
                    out.write(chunk)
            extracted_files += 1

    return ExtractionResult(
        file_count=extracted_files,
        total_bytes=written,
        skipped_special=skipped,
    )


def make_read_only(directory: Path) -> None:
    """Remove write permission so later stages cannot modify the snapshot.

    On Windows the read-only attribute is the closest available equivalent.
    """
    for path in sorted(directory.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir():
            path.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
        else:
            path.chmod(stat.S_IRUSR | stat.S_IRGRP)
    directory.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)


def remove_tree(path: Path) -> None:
    """Delete a snapshot even when it was made read-only."""
    if not path.exists():
        return
    for item in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        try:
            item.chmod(stat.S_IRWXU)
        except OSError:
            pass
    shutil.rmtree(path, ignore_errors=True)
