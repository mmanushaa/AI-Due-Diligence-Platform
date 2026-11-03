"""Evidence accuracy: do the cited lines contain the claimed thing?

Matching already guarantees the right file, an overlapping range and the right
category, so evidence accuracy has to answer a stricter question: is the line the
detector quoted actually the line that carries the issue?

The check reads the cited lines back from the fixture snapshot rather than
trusting the stored snippet, because a snippet is masked for secrets and can be
truncated. Three rules are used, in order:

    sink_pattern   the cited lines must match the issue's regex (a query built
                   with formatting, the handler that skips authentication, ...)
    secret         the cited lines must contain a token shape whose prefix the
                   issue describes (secrets are stored masked, so the snippet
                   cannot prove this)
    package        the cited lines must name the package and, when given, its
                   version (dependency and license issues)

Anything else falls back to "the cited range overlaps the issue", which matching
already established, and is therefore reported honestly rather than silently
scored as correct.

Nothing in here prints or stores a cited line: fixture secrets must not leak into
a report, a log or the ``eval_runs`` row.
"""

from __future__ import annotations

import re
from pathlib import Path

from benchmark.harness.expected import ExpectedIssue
from benchmark.harness.matching import ReportedFinding, Span, same_file

TOKEN_PATTERN = re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b|\bsk-[A-Za-z0-9]{20,}\b|\bAKIA[0-9A-Z]{16}\b")
MAX_CITED_BYTES = 2_000_000


def cited_lines(snapshot_root: Path, span: Span) -> list[str]:
    """Read the quoted lines from the snapshot, never escaping the snapshot root."""
    root = Path(snapshot_root).resolve()
    target = (root / span.file_path.replace("\\", "/")).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return []
    if not target.is_file() or target.stat().st_size > MAX_CITED_BYTES:
        return []
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, span.line_start)
    end = min(len(lines), max(start, span.line_end))
    return lines[start - 1 : end]


def _token_prefixes(issue: ExpectedIssue) -> tuple[str, ...]:
    """The token prefixes the issue's notes mention, e.g. ``ghp_``."""
    return tuple(sorted(set(re.findall(r"\b(?:ghp|gho|ghu|ghs|ghr)_|\bsk-|\bAKIA", issue.notes))))


def _check_sink(issue: ExpectedIssue, text: str) -> bool:
    try:
        return re.search(issue.sink_pattern, text) is not None
    except re.error:
        # A bad pattern in the manifest is a manifest bug, not a detector's fault;
        # treat it as unverifiable rather than as a pass.
        return False


def _check_secret(issue: ExpectedIssue, lines: list[str]) -> bool:
    prefixes = _token_prefixes(issue)
    for line in lines:
        for match in TOKEN_PATTERN.finditer(line):
            token = match.group(0)
            if not prefixes or any(token.startswith(prefix) for prefix in prefixes):
                return True
    return False


def _check_package(issue: ExpectedIssue, text: str) -> bool:
    if not issue.package:
        return False
    haystack = text.lower()
    if issue.package.lower() not in haystack:
        return False
    return issue.version is None or issue.version.lower() in haystack


def _check_overlap(issue: ExpectedIssue, span: Span) -> bool:
    return same_file(issue.file, span.file_path) and (
        issue.line_start <= span.line_end and span.line_start <= issue.line_end
    )


def evidence_is_correct(issue: ExpectedIssue, finding: ReportedFinding, snapshot_root: Path) -> bool:
    """Whether every claim the issue makes about its location is borne out."""
    for span in finding.evidence:
        if not same_file(issue.file, span.file_path):
            continue
        lines = cited_lines(Path(snapshot_root), span)
        if not lines:
            continue
        text = "\n".join(lines)

        if issue.sink_pattern:
            if _check_sink(issue, text):
                return True
            continue
        if issue.category.lower() == "secret":
            if _check_secret(issue, lines):
                return True
            continue
        if issue.package:
            if _check_package(issue, text):
                return True
            continue
        if _check_overlap(issue, span):
            return True
    return False
