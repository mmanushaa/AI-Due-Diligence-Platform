"""The matching rule.

A reported finding matches an expected issue when all three hold:

    same category        the enum value, compared case-insensitively
    same file            after normalising separators; the reported path may carry
                         the snapshot root, so a suffix match is allowed
    overlapping lines    the two ranges share at least one line

An issue that names its cycle files (``cycle_files`` in the manifest) matches by a
second rule, because a cycle spans several files and the ordinary rule would score
a correct report as a miss for citing the other end of it: the category must
agree, the finding may cite *any* member file at *any* line, and its title or
reasoning must say "cycle", "cyclic" or "circular". The wording check is what
stops an unrelated architecture finding in a cycle file, such as an oversized
module, from being counted as the cycle. Every other issue keeps the rule above.

Nothing else is inferred. A finding that cites the wrong file, the wrong region or
the wrong category is a miss *and* a false positive, which is the point: the
benchmark measures the location, not the vague topic.

Assignment is one-to-one and deterministic: expected issues are visited in
manifest order and each takes the first still-unused finding that matches, so one
finding can never satisfy two issues and one issue can never be satisfied twice.
The loader hands findings over strongest severity first, so when several
advisories share a line the report is judged by its most serious one.

The findings left over then split in two, which matters for precision:

    duplicate       matches a planted issue that another finding already satisfied.
                    A scanner reporting one row per advisory (OSV does this) or one
                    row per rule alias is reporting the same issue twice, not
                    inventing a new problem, so it is not a false positive.
    false positive  matches no planted issue at all: wrong file, wrong lines or
                    wrong category.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from benchmark.harness.expected import ExpectedIssue


def normalise_path(raw: str) -> str:
    """Comparable form of a repository-relative path."""
    parts = [part for part in PurePosixPath(raw.replace("\\", "/")).parts if part not in ("", ".")]
    return "/".join(parts)


def same_file(expected_path: str, reported_path: str) -> bool:
    """Whether two paths name the same file inside a fixture repository."""
    expected = normalise_path(expected_path)
    reported = normalise_path(reported_path)
    if expected == reported:
        return True
    # The scanner writes paths relative to the snapshot mount; keep a suffix match
    # so "app/config.py" also matches "<root>/app/config.py".
    return reported.endswith("/" + expected)


def overlaps(first_start: int, first_end: int, second_start: int, second_end: int) -> bool:
    """Whether two inclusive line ranges share a line."""
    return first_start <= second_end and second_start <= first_end


@dataclass(frozen=True)
class Span:
    """One cited location: a file and an inclusive line range."""

    file_path: str
    line_start: int
    line_end: int


@dataclass(frozen=True)
class ReportedFinding:
    """A stored finding, reduced to what the matching rule needs.

    ``evidence`` is ordered by the loader; the first span is the finding's
    location, which is what the report and the API show.
    """

    category: str
    severity: str
    evidence: tuple[Span, ...]
    title: str = ""
    # The finding's own explanation, when the pipeline stored one. The cycle rule
    # reads it so a finding can say "circular dependency" in prose rather than in
    # its one-line title.
    reasoning: str = ""

    @property
    def location(self) -> Span | None:
        """The finding's primary location, or ``None`` when it cites nothing."""
        return self.evidence[0] if self.evidence else None

    def describe(self) -> str:
        """One-line label used in the report's false-positive list."""
        location = self.location
        where = "no evidence" if location is None else (
            f"{location.file_path}:{location.line_start}-{location.line_end}"
        )
        return f"{self.category} at {where}"


SEVERITY_RANK = {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}


def order_findings(findings: tuple[ReportedFinding, ...] | list[ReportedFinding]) -> tuple[ReportedFinding, ...]:
    """Deterministic, strongest-first order.

    One dependency line can carry several advisories of different severities and
    the database has no stable order between them, so the order is fixed here:
    severity descending, then category, file, line and title. That makes matching
    reproducible and lets the most serious report satisfy the expected issue, which
    is the reading a reviewer takes from a report.
    """
    return tuple(
        sorted(
            findings,
            key=lambda item: (
                -SEVERITY_RANK.get(item.severity.lower(), 0),
                item.category,
                item.location.file_path if item.location else "",
                item.location.line_start if item.location else 0,
                item.title,
            ),
        )
    )


@dataclass(frozen=True)
class Assignment:
    """One expected issue and the finding that satisfied it."""

    issue: ExpectedIssue
    finding: ReportedFinding


@dataclass(frozen=True)
class MatchOutcome:
    """The full result of comparing one repository's findings with the manifest."""

    assignments: tuple[Assignment, ...]
    missed: tuple[ExpectedIssue, ...]
    duplicates: tuple[ReportedFinding, ...]
    false_positives: tuple[ReportedFinding, ...]

    @property
    def distinct_reported(self) -> int:
        """Findings that report something new: matched plus false positives."""
        return len(self.assignments) + len(self.false_positives)


CYCLE_WORDS = ("cycle", "cyclic", "circular")


def mentions_cycle(finding: ReportedFinding) -> bool:
    """Whether the finding says, in its own words, that it reports a cycle."""
    text = f"{finding.title} {finding.reasoning}".lower()
    return any(word in text for word in CYCLE_WORDS)


def matches(issue: ExpectedIssue, finding: ReportedFinding) -> bool:
    """Whether one finding reports this expected issue."""
    if issue.category.lower() != finding.category.lower():
        return False
    if issue.cycle_files:
        if not mentions_cycle(finding):
            return False
        return any(
            same_file(member, span.file_path)
            for span in finding.evidence
            for member in issue.cycle_files
        )
    for span in finding.evidence:
        if not same_file(issue.file, span.file_path):
            continue
        if overlaps(issue.line_start, issue.line_end, span.line_start, span.line_end):
            return True
    return False


def match_all(
    issues: tuple[ExpectedIssue, ...] | list[ExpectedIssue],
    findings: tuple[ReportedFinding, ...] | list[ReportedFinding],
) -> MatchOutcome:
    """Assign each expected issue at most one finding, and never reuse a finding."""
    used: set[int] = set()
    assignments: list[Assignment] = []
    missed: list[ExpectedIssue] = []

    for issue in issues:
        chosen = None
        for index, finding in enumerate(findings):
            if index in used:
                continue
            if matches(issue, finding):
                chosen = index
                break
        if chosen is None:
            missed.append(issue)
        else:
            used.add(chosen)
            assignments.append(Assignment(issue=issue, finding=findings[chosen]))

    duplicates: list[ReportedFinding] = []
    false_positives: list[ReportedFinding] = []
    for index, finding in enumerate(findings):
        if index in used:
            continue
        # An issue that is already satisfied: this is a second report of it.
        if any(matches(issue, finding) for issue in issues):
            duplicates.append(finding)
        else:
            false_positives.append(finding)

    return MatchOutcome(
        assignments=tuple(assignments),
        missed=tuple(missed),
        duplicates=tuple(duplicates),
        false_positives=tuple(false_positives),
    )
