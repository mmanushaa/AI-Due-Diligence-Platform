"""Risk scoring arithmetic (step 3.3), with no database and no model.

Every number here is a constant a reviewer can read, and the level is derived in two
explicit stages:

1. **The floor.** The audit is never rated below the worst counted finding. One
   verified critical among a hundred lows is still critical; a finding never gets
   diluted by the ones around it.
2. **Escalation.** A short, named list of rules can raise the level above the floor
   (a confirmed credential, many highs at once, a restrictive license, a large
   aggregate score). Nothing can lower it.

The aggregate score is a **sum**, not an average: averaging is how a platform talks
itself out of a critical finding by adding noise around it. A finding's contribution
is its severity weight times how well it is evidenced times how exposed it is, and
those three numbers are stored per finding so the total can be rebuilt by hand. The
exposure multiplier starts from the verification status and is adjusted by hints that
are stored on the finding itself (a committed secret is more exposed; evidence that
lives only in tests is less), never by a model's guess.

Severity is an ordinal judgement, not a measurement, so the level - not the score -
is what the platform acts on. The score exists to rank work and to trip escalation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import PurePosixPath

from app.db.enums import Category, Severity, VerificationStatus

#: What one finding of each severity is worth before evidence and exposure. The gaps
#: are wide on purpose: critical outranks high by more than high outranks medium.
SEVERITY_WEIGHT: dict[Severity, Decimal] = {
    Severity.CRITICAL: Decimal("10"),
    Severity.HIGH: Decimal("7"),
    Severity.MEDIUM: Decimal("4"),
    Severity.LOW: Decimal("1.5"),
    Severity.INFO: Decimal("0.5"),
}

#: How much of the claim the evidence actually supports. An unresolved finding is
#: still risk - it is reported and counted - but it contributes less until checked.
EXPOSURE: dict[VerificationStatus, Decimal] = {
    VerificationStatus.VERIFIED: Decimal("1.0"),
    VerificationStatus.SCANNER_CONFIRMED: Decimal("1.0"),
    VerificationStatus.AI_SUGGESTED: Decimal("0.8"),
    VerificationStatus.UNRESOLVED: Decimal("0.6"),
}
DEFAULT_CONFIDENCE = Decimal("0.5")

#: A rejected claim is not risk at all.
EXCLUDED = (VerificationStatus.REJECTED_FALSE_POSITIVE,)

#: Statuses whose evidence was checked - the ones specific escalation rules trust.
CONFIRMED_STATUSES = (
    VerificationStatus.VERIFIED,
    VerificationStatus.SCANNER_CONFIRMED,
)

#: Exposure hints that are derivable from the stored finding itself. The roadmap's
#: reachability labelling (step 2.13) is optional and not stored, so this file never
#: pretends to know whether a function is called from a route: it only uses what the
#: finding already says. A hint changes how much the finding's risk is exposed.
COMMITTED_SECRET_FACTOR = Decimal("1.25")
TEST_ONLY_FACTOR = Decimal("0.5")
_TEST_PATH_PARTS = frozenset(
    {"test", "tests", "__tests__", "spec", "specs", "testdata", "fixtures"}
)
#: Title words that mark a license finding as restrictive. This is a keyword hint for
#: escalation, not a legal judgement - the compliance agent cites the actual license.
COPYLEFT_HINTS = ("gpl", "agpl", "sspl", "copyleft")

SEVERITY_ORDER = (
    Severity.CRITICAL,
    Severity.HIGH,
    Severity.MEDIUM,
    Severity.LOW,
    Severity.INFO,
)

#: Three unrelated highs are an audit-wide problem even without a critical.
MANY_HIGH_COUNT = 3
#: Sum at which the aggregate alone argues for more than the floor says.
AGGREGATE_ESCALATION = Decimal("20")


@dataclass(frozen=True)
class FindingInput:
    """The only fields scoring reads from a finding.

    ``category``, ``title`` and ``evidence_paths`` feed the exposure hints and the
    escalation rules. They are stored fields, never a model's opinion.
    """

    severity: Severity
    confidence: Decimal | float | None
    status: VerificationStatus
    category: Category | None = None
    title: str = ""
    evidence_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class FindingScore:
    severity_weight: Decimal
    evidence_confidence: Decimal
    exposure_multiplier: Decimal
    score: Decimal
    hints: tuple[str, ...] = ()

    @property
    def notes(self) -> str:
        hint_text = f" [{', '.join(self.hints)}]" if self.hints else ""
        return (
            f"{self.severity_weight} weight × {self.evidence_confidence} confidence × "
            f"{self.exposure_multiplier} exposure = {self.score}{hint_text}"
        )


@dataclass(frozen=True)
class Score:
    """The result of scoring one audit's findings."""

    level: Severity
    floor: Severity
    score: Decimal
    counts: dict[str, int]
    per_finding: list[FindingScore] = field(default_factory=list)
    rules: tuple[str, ...] = ()
    rationale: str = ""

    @property
    def rule_applied(self) -> str:
        base = "severity_floor"
        return base if not self.rules else base + "+" + "+".join(self.rules)


def _four(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _clamp(value: Decimal) -> Decimal:
    return max(Decimal("0"), min(Decimal("1"), value))


def _test_only(path: str) -> bool:
    return any(part.lower() in _TEST_PATH_PARTS for part in PurePosixPath(path.replace("\\", "/")).parts)


def exposure_hints(finding: FindingInput) -> tuple[str, ...]:
    """The reachability hints the stored finding supports, named for the report."""
    hints: list[str] = []
    if finding.category is Category.SECRET:
        hints.append("committed_secret")
    paths = finding.evidence_paths
    if paths and all(_test_only(path) for path in paths):
        hints.append("test_only_evidence")
    return tuple(hints)


def exposure_multiplier(finding: FindingInput, hints: tuple[str, ...]) -> Decimal:
    multiplier = EXPOSURE.get(finding.status, Decimal("0.6"))
    if "committed_secret" in hints:
        multiplier *= COMMITTED_SECRET_FACTOR
    if "test_only_evidence" in hints:
        multiplier *= TEST_ONLY_FACTOR
    return _four(multiplier)


def score_finding(finding: FindingInput) -> FindingScore:
    weight = SEVERITY_WEIGHT[finding.severity]
    confidence = DEFAULT_CONFIDENCE if finding.confidence is None else Decimal(str(finding.confidence))
    confidence = _clamp(confidence)
    hints = exposure_hints(finding)
    exposure = exposure_multiplier(finding, hints)
    return FindingScore(
        severity_weight=weight,
        evidence_confidence=_four(confidence),
        exposure_multiplier=exposure,
        score=_four(weight * confidence * exposure),
        hints=hints,
    )


def highest_severity(levels: list[Severity]) -> Severity:
    """The worst level in the list; ``info`` when it is empty."""
    for candidate in SEVERITY_ORDER:
        if candidate in levels:
            return candidate
    return Severity.INFO


def _escalate_one(level: Severity) -> Severity:
    index = SEVERITY_ORDER.index(level)
    return SEVERITY_ORDER[max(0, index - 1)]


RulePredicate = Callable[
    [list[FindingInput], dict[str, int], Decimal, Severity], bool
]


def _rule_many_high(
    findings: list[FindingInput], counts: dict[str, int], score: Decimal, floor: Severity
) -> bool:
    return counts.get(Severity.HIGH.value, 0) >= MANY_HIGH_COUNT


def _rule_aggregate(
    findings: list[FindingInput], counts: dict[str, int], score: Decimal, floor: Severity
) -> bool:
    """Volume lifts a quiet audit by one level, but cannot manufacture a critical.

    Fifty low findings next to a high one add up to a large sum; that should not turn
    a high audit critical, or the aggregate becomes an average by another name. Only a
    critical finding (the floor), three highs or a confirmed credential may reach
    critical.
    """
    return score >= AGGREGATE_ESCALATION and floor not in (Severity.CRITICAL, Severity.HIGH)


def _rule_confirmed_credential(
    findings: list[FindingInput], counts: dict[str, int], score: Decimal, floor: Severity
) -> bool:
    """A secret found in the code and confirmed is exposed, whatever its severity tag."""
    return any(
        finding.category is Category.SECRET and finding.status in CONFIRMED_STATUSES
        for finding in findings
    )


def _rule_copyleft_license(
    findings: list[FindingInput], counts: dict[str, int], score: Decimal, floor: Severity
) -> bool:
    """A confirmed restrictive license raises the level by one, never to critical alone.

    Whether the product is distributed is a business fact a repository cannot prove, so
    this rule escalates one step and the rationale says why; it does not decide the
    legal outcome.
    """
    return any(
        finding.category is Category.LICENSE
        and finding.status in CONFIRMED_STATUSES
        and any(word in finding.title.lower() for word in COPYLEFT_HINTS)
        for finding in findings
    )


#: Named in ``rule_applied`` and in the rationale, in the order they are applied.
ESCALATION_RULES: tuple[tuple[str, str, RulePredicate], ...] = (
    ("confirmed_credential", "critical", _rule_confirmed_credential),
    ("many_high_findings", "critical", _rule_many_high),
    ("copyleft_license", "escalate_one", _rule_copyleft_license),
    ("aggregate_score", "escalate_one", _rule_aggregate),
)


def evaluate(findings: list[FindingInput]) -> Score:
    """Score every finding and derive the audit level without averaging."""
    counted = [f for f in findings if f.status not in EXCLUDED]
    per_finding = [score_finding(f) for f in counted]
    total = _four(sum((item.score for item in per_finding), Decimal("0")))

    counts: dict[str, int] = {severity.value: 0 for severity in SEVERITY_ORDER}
    for finding in counted:
        counts[finding.severity.value] += 1

    floor = highest_severity([f.severity for f in counted])
    level = floor
    fired: list[str] = []
    for name, effect, predicate in ESCALATION_RULES:
        if not predicate(counted, counts, total, floor):
            continue
        fired.append(name)
        if effect == "escalate_one":
            level = _escalate_one(level)
        else:
            level = highest_severity([level, Severity(str(effect))])

    fired_text = (
        f" Escalation rule(s) applied: {', '.join(fired)}."
        if fired
        else " No escalation rule applied."
    )
    rationale = (
        f"{len(counted)} finding(s) counted, {len(findings) - len(counted)} rejected by "
        f"validation; aggregate score {total} (sum, not average); the highest counted "
        f"severity is {floor.value}."
        + fired_text
    )
    return Score(
        level=level,
        floor=floor,
        score=total,
        counts=counts,
        per_finding=per_finding,
        rules=tuple(fired),
        rationale=rationale,
    )


__all__ = [
    "AGGREGATE_ESCALATION",
    "CONFIRMED_STATUSES",
    "COPYLEFT_HINTS",
    "DEFAULT_CONFIDENCE",
    "ESCALATION_RULES",
    "EXCLUDED",
    "EXPOSURE",
    "SEVERITY_WEIGHT",
    "FindingInput",
    "FindingScore",
    "Score",
    "evaluate",
    "exposure_hints",
    "exposure_multiplier",
    "highest_severity",
    "score_finding",
]
