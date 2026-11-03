"""Make ``app`` importable when the harness runs as a script.

The backend is a separate directory from this harness, and the project does not
install it as a package, so the repository's ``backend/`` has to be on
``sys.path`` before anything imports ``app.*``. Doing it here (instead of at the
call site) keeps every entry point — ``python -m benchmark.harness``, pytest, a
future notebook — working the same way.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "backend"


def add_backend_to_path() -> Path:
    """Put ``backend/`` first on ``sys.path`` (idempotent) and return it."""
    resolved = str(BACKEND)
    if resolved not in sys.path:
        sys.path.insert(0, resolved)
    return BACKEND
