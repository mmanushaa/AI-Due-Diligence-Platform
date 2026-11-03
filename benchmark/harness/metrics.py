"""The numbers, kept together with the counts that produce them.

Every rate here is derived from two integers that are also stored, so a reader can
recompute any figure by hand instead of trusting the decimal:

    recall               matched / expected
    precision            matched / (matched + false positives)
    false-positive rate  false positives / (matched + false positives)
    evidence accuracy    evidence_correct / matched
    severity accuracy    severity_correct / matched
    cross-file recall    cross_file_matched / cross_file_expected

A rate whose denominator is zero is reported as ``None`` ("n/a") rather than as
0.0 or 1.0: a clean repository has no expected issues, and scoring that as a
perfect recall would hide that nothing was measured.

The precision denominator is *distinct reported issues*, not raw finding rows:
`dup` counts the rows that repeat an already-matched issue (OSV writes one row
per advisory, so a dependency with four advisories yields four rows for one
issue). Counting those as false positives would measure an adapter's row shape
instead of its accuracy. Raw row counts are kept in `findings` and `duplicates`
so nothing is hidden.

The alternative false-positive definition (false positives over all non-issues)
would need a count of every possible issue in the fixture, which does not exist;
this one is what a reviewer can actually check.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

RATE_PLACES = 4


def _rate(numerator: int, denominator: int) -> float | None:
    """A rate, or ``None`` when nothing could be measured."""
    if denominator <= 0:
        return None
    return round(numerator / denominator, RATE_PLACES)


def _fraction(numerator: int, denominator: int) -> str:
    """The division behind a rate, for the report's arithmetic section."""
    if denominator <= 0:
        return "n/a"
    return f"{numerator}/{denominator}"


@dataclass(frozen=True)
class RepoScore:
    """One repository's contribution, plus every count the rates use."""

    repo: str
    expected: int
    findings: int
    matched: int
    duplicates: int
    evidence_correct: int
    severity_correct: int
    cross_file_expected: int
    cross_file_matched: int
    cost_usd: Decimal
    runtime_ms: int
    matched_ids: tuple[str, ...] = ()
    missed_ids: tuple[str, ...] = ()
    false_positives: tuple[str, ...] = ()

    @property
    def recall(self) -> float | None:
        """Share of the planted issues that were reported at the right place."""
        return _rate(self.matched, self.expected)

    @property
    def false_positive_count(self) -> int:
        """Findings that match no planted issue at all."""
        return self.findings - self.matched - self.duplicates

    @property
    def distinct_reported(self) -> int:
        """Distinct issues reported: matched plus false positives."""
        return self.matched + self.false_positive_count

    @property
    def precision(self) -> float | None:
        """Share of the distinct reported issues that match a planted issue."""
        return _rate(self.matched, self.distinct_reported)

    @property
    def false_positive_rate(self) -> float | None:
        """Share of the distinct reported issues that match nothing planted."""
        return _rate(self.false_positive_count, self.distinct_reported)

    @property
    def evidence_accuracy(self) -> float | None:
        """Share of the matched findings whose cited lines hold the claimed thing."""
        return _rate(self.evidence_correct, self.matched)

    @property
    def severity_accuracy(self) -> float | None:
        """Share of the matched findings whose severity equals the expected one."""
        return _rate(self.severity_correct, self.matched)

    @property
    def cross_file_recall(self) -> float | None:
        """Share of the cross-file issues that were found."""
        return _rate(self.cross_file_matched, self.cross_file_expected)

    def fractions(self) -> dict[str, str]:
        """Every rate as the division that produced it."""
        return {
            "recall": _fraction(self.matched, self.expected),
            "precision": _fraction(self.matched, self.distinct_reported),
            "false_positive_rate": _fraction(self.false_positive_count, self.distinct_reported),
            "evidence_accuracy": _fraction(self.evidence_correct, self.matched),
            "severity_accuracy": _fraction(self.severity_correct, self.matched),
            "cross_file_recall": _fraction(self.cross_file_matched, self.cross_file_expected),
        }

    def to_json(self) -> dict[str, object]:
        """JSONB-friendly form; no file contents, snippets or secrets."""
        return {
            "repo": self.repo,
            "expected": self.expected,
            "findings": self.findings,
            "matched": self.matched,
            "duplicates": self.duplicates,
            "false_positives": self.false_positive_count,
            "distinct_reported": self.distinct_reported,
            "evidence_correct": self.evidence_correct,
            "severity_correct": self.severity_correct,
            "cross_file_expected": self.cross_file_expected,
            "cross_file_matched": self.cross_file_matched,
            "recall": self.recall,
            "precision": self.precision,
            "false_positive_rate": self.false_positive_rate,
            "evidence_accuracy": self.evidence_accuracy,
            "severity_accuracy": self.severity_accuracy,
            "cross_file_recall": self.cross_file_recall,
            "cost_usd": str(self.cost_usd),
            "runtime_ms": self.runtime_ms,
            "matched_ids": list(self.matched_ids),
            "missed_ids": list(self.missed_ids),
            "false_positive_findings": list(self.false_positives),
        }


def totals(scores: tuple[RepoScore, ...]) -> RepoScore:
    """Summation across repositories: counts add, rates are recomputed from them."""
    return RepoScore(
        repo="TOTAL",
        expected=sum(score.expected for score in scores),
        findings=sum(score.findings for score in scores),
        matched=sum(score.matched for score in scores),
        duplicates=sum(score.duplicates for score in scores),
        evidence_correct=sum(score.evidence_correct for score in scores),
        severity_correct=sum(score.severity_correct for score in scores),
        cross_file_expected=sum(score.cross_file_expected for score in scores),
        cross_file_matched=sum(score.cross_file_matched for score in scores),
        cost_usd=sum((score.cost_usd for score in scores), Decimal("0")),
        runtime_ms=sum(score.runtime_ms for score in scores),
        matched_ids=tuple(issue_id for score in scores for issue_id in score.matched_ids),
        missed_ids=tuple(issue_id for score in scores for issue_id in score.missed_ids),
        false_positives=tuple(item for score in scores for item in score.false_positives),
    )
