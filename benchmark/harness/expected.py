"""The benchmark manifest.

``benchmark/expected.json`` is the ground truth: every entry names a real file in
a fixture repository and the exact lines the issue occupies. This module only
parses it, so a malformed manifest fails loudly at start-up rather than producing
a quietly wrong precision number.

A repository with no entries is a *clean control*: it must produce no findings, so
anything reported there is a false positive by construction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

REQUIRED_FIELDS = ("id", "repo", "category", "title", "file", "line_start", "line_end", "severity")


class ManifestError(ValueError):
    """The manifest is missing, malformed or internally inconsistent."""


@dataclass(frozen=True)
class ExpectedIssue:
    """One planted issue, with the location it must be reported at."""

    id: str
    repo: str
    category: str
    title: str
    file: str
    line_start: int
    line_end: int
    severity: str
    detector: str
    cross_file: bool = False
    sink_pattern: str | None = None
    package: str | None = None
    version: str | None = None
    notes: str = ""

    @property
    def path(self) -> PurePosixPath:
        """The issue's file as a POSIX path, so Windows separators never matter."""
        return PurePosixPath(self.file.replace("\\", "/"))

    def describe(self) -> str:
        """One-line label used in the report's arithmetic section."""
        return f"{self.id} ({self.file}:{self.line_start}-{self.line_end}, {self.category})"


@dataclass(frozen=True)
class Manifest:
    """The manifest plus the fixture repositories discovered next to it."""

    version: str
    issues: tuple[ExpectedIssue, ...]
    repos: tuple[str, ...]

    def for_repo(self, repo: str) -> tuple[ExpectedIssue, ...]:
        """Every expected issue in one repository, in manifest order."""
        return tuple(issue for issue in self.issues if issue.repo == repo)

    @property
    def cross_file_issues(self) -> tuple[ExpectedIssue, ...]:
        """Issues that cannot be seen from a single file."""
        return tuple(issue for issue in self.issues if issue.cross_file)


def discover_repos(repos_root: Path) -> tuple[str, ...]:
    """Return every fixture directory that contains at least one file."""
    if not repos_root.is_dir():
        raise ManifestError(f"fixture directory {repos_root} does not exist")
    return tuple(
        sorted(
            child.name
            for child in repos_root.iterdir()
            if child.is_dir() and any(path.is_file() for path in child.rglob("*"))
        )
    )


def _as_issue(raw: dict) -> ExpectedIssue:
    missing = [field for field in REQUIRED_FIELDS if field not in raw]
    if missing:
        raise ManifestError(f"issue {raw.get('id', '<no id>')} is missing {', '.join(missing)}")
    if raw["line_start"] > raw["line_end"]:
        raise ManifestError(f"issue {raw['id']} has line_start after line_end")
    return ExpectedIssue(
        id=str(raw["id"]),
        repo=str(raw["repo"]),
        category=str(raw["category"]),
        title=str(raw["title"]),
        file=str(raw["file"]),
        line_start=int(raw["line_start"]),
        line_end=int(raw["line_end"]),
        severity=str(raw["severity"]),
        detector=str(raw.get("detector", "")),
        cross_file=bool(raw.get("cross_file", False)),
        sink_pattern=raw.get("sink_pattern"),
        package=raw.get("package"),
        version=raw.get("version"),
        notes=str(raw.get("notes", "")),
    )


def load_manifest(path: Path, repos_root: Path | None = None) -> Manifest:
    """Read and validate the manifest, and check it agrees with the fixtures."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"manifest {path} does not exist") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest {path} is not valid JSON: {exc}") from exc

    issues = tuple(_as_issue(item) for item in raw.get("issues", ()))
    seen: set[str] = set()
    for issue in issues:
        if issue.id in seen:
            raise ManifestError(f"duplicate issue id {issue.id}")
        seen.add(issue.id)

    root = Path(repos_root) if repos_root is not None else path.parent / "repos"
    repos = discover_repos(root)
    for issue in issues:
        if issue.repo not in repos:
            raise ManifestError(f"issue {issue.id} names unknown repository {issue.repo}")
        target = root / issue.repo / issue.file
        if not target.is_file():
            raise ManifestError(f"issue {issue.id} cites {issue.file}, which is not in {issue.repo}")
        line_count = len(target.read_text(encoding="utf-8", errors="replace").splitlines())
        if issue.line_end > line_count:
            raise ManifestError(
                f"issue {issue.id} cites line {issue.line_end} but {issue.file} has {line_count}"
            )

    return Manifest(
        version=str(raw.get("benchmark_version", "unknown")), issues=issues, repos=repos
    )
