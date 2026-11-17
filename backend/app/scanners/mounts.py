"""Translate container paths to paths the host docker daemon can bind-mount.

Scanner containers are started through the host daemon (see `docker-compose.yml`),
but the worker sees the data directory at a path inside its own container. On a
Linux host the two paths happen to agree; with Docker Desktop they never do, and a
snapshot path such as `/srv/data/uploads/...` is not mountable by the daemon. The
worker therefore receives the host path of the same directory as HOST_DATA_DIR and
the path it sees itself as CONTAINER_DATA_DIR; scanner bind sources are rewritten
with that pair, while the in-container target (SNAPSHOT_MOUNT etc.) stays as is.
"""

from __future__ import annotations

import os
from pathlib import Path


def docker_source(path: Path | str) -> str:
    """The bind-mount source for `path`, as the host docker daemon must see it."""
    resolved = Path(path).resolve().as_posix()
    host_root = os.environ.get("HOST_DATA_DIR")
    container_root = os.environ.get("CONTAINER_DATA_DIR", "/srv/data")
    if not host_root:
        return resolved
    host_root = host_root.rstrip("/")
    container_root = container_root.rstrip("/")
    if container_root and (resolved == container_root or resolved.startswith(container_root + "/")):
        suffix = resolved[len(container_root) :]
        return f"{host_root}{suffix}"
    return resolved


def prepare_output_dir(path: Path | str) -> Path:
    """Create the scanner's output directory so the scanner user can write to it.

    The directory is on a bind mount shared with the scanner container, whose image
    runs as its own non-root user; the worker creates it as root, so the mode has to
    be opened up explicitly. Failure to chmod (Windows host) is not fatal: there the
    daemon rewrites ownership itself.
    """
    import stat as _stat

    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(
            _stat.S_IRWXU | _stat.S_IRWXG | _stat.S_IRWXO
        )
    except OSError:
        pass
    return directory
