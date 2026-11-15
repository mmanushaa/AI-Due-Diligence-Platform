"""The report document (step 3.9).

A report is built as data first and rendered second. The data model is deliberately
small and closed: every sentence that makes a claim about the audit carries the ids of
the findings that support it, so the claim can be checked against the database before
any HTML exists. ``ReportDocument.citations_dropped`` records the claims that failed
that check - the report still renders, with a visible warning, because a report that
silently drops a wrong sentence is worse than one that says it did.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Claim:
    """One sentence of the executive summary and the findings behind it."""

    text: str
    finding_ids: tuple[str, ...]

    @property
    def cites(self) -> bool:
        return bool(self.finding_ids)


@dataclass(frozen=True)
class DroppedClaim:
    """A claim whose citation did not resolve, kept so the report can say so."""

    text: str
    unknown_ids: tuple[str, ...]


@dataclass(frozen=True)
class AuditView:
    """What the report may say about the audit itself. Nothing else is read."""

    audit_id: str
    repository: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    spent_usd: str
    budget_usd: str | None


@dataclass(frozen=True)
class FindingView:
    id: str
    title: str
    category: str
    severity: str
    status: str
    verdict: str | None
    confidence: float | None
    file_path: str | None
    line_start: int | None
    line_end: int | None
    recommendation: str | None
    evidence_lines: int


@dataclass(frozen=True)
class RiskView:
    level: str
    rule: str
    rationale: str | None
    score: float | None
    supersedes_id: str | None


@dataclass(frozen=True)
class CategoryRow:
    category: str
    counts: dict[str, int]
    total: int


@dataclass(frozen=True)
class ReviewView:
    decision: str
    reason: str | None
    original_level: str | None
    final_level: str | None
    reviewer: str | None
    decided_at: datetime


@dataclass
class ReportDocument:
    audit: AuditView
    risk: RiskView | None
    claims: list[Claim]
    top_findings: list[FindingView]
    categories: list[CategoryRow]
    priorities: list[FindingView]
    limitations: list[str]
    review: ReviewView | None
    totals: dict[str, int]
    citations_dropped: list[DroppedClaim] = field(default_factory=list)
    generated_at: datetime | None = None

    @property
    def has_dropped_citations(self) -> bool:
        return bool(self.citations_dropped)

    @property
    def severity_order(self) -> list[str]:
        order = ["critical", "high", "medium", "low", "info"]
        return [name for name in order if name in self.totals]
