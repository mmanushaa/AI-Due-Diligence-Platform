"""Every file-filtering rule lives here (step 1.3).

Change the skip lists in this file; nothing else hardcodes a folder or
extension.
"""

from dataclasses import dataclass, field
from typing import Mapping

# Directory names that appear anywhere in a path. `vendor` and `.git` are
# vendored/version-control content; `dist`, `build` and `__pycache__` are
# generated output.
VENDORED_DIRECTORIES: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "vendor",
        "bower_components",
        ".tox",
        "site-packages",
    }
)

BUILD_OUTPUT_DIRECTORIES: frozenset[str] = frozenset(
    {
        "dist",
        "build",
        "out",
        "target",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".next",
        ".nuxt",
        "coverage",
        "htmlcov",
        ".eggs",
    }
)

# Extensions that are binary even when a sample happens to contain no null byte.
BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".7z", ".a", ".avi", ".bin", ".bmp", ".bz2", ".class", ".db", ".dll",
        ".dmg", ".doc", ".docx", ".eot", ".exe", ".flac", ".gif", ".gz", ".ico",
        ".iso", ".jar", ".jpeg", ".jpg", ".lockb", ".m4a", ".mov", ".mp3", ".mp4",
        ".o", ".otf", ".pdf", ".pickle", ".pkl", ".png", ".pyc", ".pyd", ".pyo",
        ".so", ".sqlite", ".sqlite3", ".tar", ".tiff", ".ttf", ".wav", ".webm",
        ".webp", ".whl", ".woff", ".woff2", ".xls", ".xlsx", ".xz", ".zip",
    }
)

# Exact file names that map to a language independent of their extension.
LANGUAGE_BY_FILENAME: Mapping[str, str] = {
    "dockerfile": "dockerfile",
    "makefile": "makefile",
    ".dockerignore": "ignore-list",
    ".gitignore": "ignore-list",
    ".env.example": "dotenv",
}

# Extension -> language for the v1 languages plus common support files.
LANGUAGE_BY_EXTENSION: Mapping[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".json": "json",
    ".jsonc": "json",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".md": "markdown",
    ".markdown": "markdown",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "css",
    ".sql": "sql",
    ".sh": "shell",
    ".bash": "shell",
    ".txt": "text",
    ".rst": "text",
    ".env": "dotenv",
    ".example": "text",
}

DEFAULT_MAX_FILE_BYTES = 1_048_576  # 1 MiB

# Reason labels stored in files.exclude_reason.
REASON_VENDORED = "vendored_directory"
REASON_BUILD = "build_output"
REASON_TOO_LARGE = "file_too_large"
REASON_BINARY_EXT = "binary_extension"
REASON_BINARY_NULL = "binary_null_byte"
REASON_SYMLINK = "symlink"


@dataclass(frozen=True)
class FilterConfig:
    """All skip lists and thresholds used by the file walker."""

    vendored_directories: frozenset[str] = field(default_factory=lambda: VENDORED_DIRECTORIES)
    build_directories: frozenset[str] = field(default_factory=lambda: BUILD_OUTPUT_DIRECTORIES)
    binary_extensions: frozenset[str] = field(default_factory=lambda: BINARY_EXTENSIONS)
    language_by_extension: Mapping[str, str] = field(
        default_factory=lambda: LANGUAGE_BY_EXTENSION
    )
    language_by_filename: Mapping[str, str] = field(default_factory=lambda: LANGUAGE_BY_FILENAME)
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    null_byte_sample_bytes: int = 8192


DEFAULT_FILTER_CONFIG = FilterConfig()
