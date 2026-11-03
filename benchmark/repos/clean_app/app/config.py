"""Configuration for the document service.

Nothing secret has a default: an absent environment variable means the feature
is off, not that a placeholder credential is used.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB_PATH = "documents.db"
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


@dataclass(frozen=True)
class Settings:
    """Runtime settings, resolved once at start-up."""

    database_path: Path
    page_size: int


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """Read settings from the environment (or from ``env`` when given)."""
    source = os.environ if env is None else env
    page_size = int(source.get("CLEAN_APP_PAGE_SIZE", DEFAULT_PAGE_SIZE))
    return Settings(
        database_path=Path(source.get("CLEAN_APP_DB", DEFAULT_DB_PATH)),
        page_size=max(1, min(page_size, MAX_PAGE_SIZE)),
    )
