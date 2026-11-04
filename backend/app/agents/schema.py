"""What an agent is allowed to return, and what happens when it does not.

Two layers, deliberately separate:

    AgentReport        the shape of the JSON: a closed set of categories and
                       severities, integer line ranges, bounded text. Pydantic
                       rejects anything else, including extra keys the model
                       invented.

    validate_report    the claims a schema cannot check. A file has to exist in
                       this audit, a line range has to fall inside the file, and a
                       quoted snippet has to appear at the cited lines. A finding
                       that fails is dropped and reported as a reason the model can
                       read, which is what makes "retry once with the errors" mean
                       something.

Nothing in here trusts the model: a schema-valid finding that cites a line that
does not exist is still rejected, because a report is judged by whether a human
could follow it to the code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.agents.tools import RepositoryTools

ALLOWED_CATEGORIES = ("security", "architecture", "license")
ALLOWED_SEVERITIES = ("critical", "high", "medium", "low", "info")
MAX_FINDINGS = 30
MAX_TITLE = 200
MAX_TEXT = 2_000
QUOTE_WINDOW = 3  # quoted text may start one line off and still be the same claim


class AgentEvidence(BaseModel):
    """Optional supporting detail: what the model says it read."""

    model_config = ConfigDict(extra="forbid")

    file: str = Field(min_length=1, max_length=400)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)

    @model_validator(mode="after")
    def _range_is_ordered(self) -> AgentEvidence:
        if self.line_end < self.line_start:
            raise ValueError("line_end is before line_start")
        return self


class AgentFinding(BaseModel):
    """One finding, in the shape the finding table needs."""

    model_config = ConfigDict(extra="forbid")

    category: Literal["security", "architecture", "license"]
    title: str = Field(min_length=8, max_length=MAX_TITLE)
    file: str = Field(min_length=1, max_length=400)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    severity: Literal["critical", "high", "medium", "low", "info"]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1, max_length=MAX_TEXT)
    recommendation: str | None = Field(default=None, max_length=MAX_TEXT)
    evidence_quote: str | None = Field(default=None, max_length=MAX_TEXT)

    @field_validator("title", "reasoning")
    @classmethod
    def _single_line(cls, value: str) -> str:
        """Collapse whitespace: a title with newlines breaks every list it lands in."""
        return " ".join(value.split())

    @field_validator("recommendation", "evidence_quote")
    @classmethod
    def _clean_optional(cls, value: str | None) -> str | None:
        return None if value is None else value.strip() or None

    @model_validator(mode="after")
    def _range_is_ordered(self) -> AgentFinding:
        if self.line_end < self.line_start:
            raise ValueError("line_end is before line_start")
        if self.line_end - self.line_start > 400:
            raise ValueError("line range is too wide to be a citation")
        return self


class AgentReport(BaseModel):
    """The whole reply: findings, or an explicit empty list."""

    model_config = ConfigDict(extra="forbid")

    findings: list[AgentFinding] = Field(default_factory=list, max_length=MAX_FINDINGS)


@dataclass(frozen=True)
class RejectedFinding:
    """A finding that did not survive validation, and why."""

    title: str
    reason: str

    def render(self) -> str:
        return f'"{self.title[:80]}": {self.reason}'


@dataclass(frozen=True)
class ValidationResult:
    """Accepted findings plus everything that was thrown away."""

    accepted: tuple[AgentFinding, ...] = ()
    rejected: tuple[RejectedFinding, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """A reply is usable when nothing was thrown away."""
        return not self.rejected


class InvalidOutput(ValueError):
    """The reply could not be read as a report at all."""


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def parse_report(raw: str) -> AgentReport:
    """Parse the model's reply into a report, or raise :class:`InvalidOutput`."""
    text = _FENCE.sub("", (raw or "").strip())
    if not text:
        raise InvalidOutput("empty reply")
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise InvalidOutput("reply contains no JSON object")
    candidate = text[start : end + 1]
    try:
        return AgentReport.model_validate_json(candidate)
    except ValidationError as exc:
        raise InvalidOutput(_summarise(exc)) from exc


def _summarise(exc: ValidationError) -> str:
    """Turn pydantic's errors into one sentence the model can act on."""
    parts: list[str] = []
    for error in exc.errors()[:5]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "report"
        parts.append(f"{location}: {error.get('msg', 'invalid')}")
    return "; ".join(parts)


def _normalise(text: str) -> str:
    return " ".join(text.split()).lower()


def _quote_matches(quote: str, lines: list[str], line_start: int, line_end: int) -> bool:
    """Whether the quote appears at the cited lines, allowing a one-line drift."""
    wanted = _normalise(quote)
    if not wanted:
        return True
    low = max(1, line_start - QUOTE_WINDOW)
    high = min(len(lines), line_end + QUOTE_WINDOW)
    window = _normalise("\n".join(lines[low - 1 : high]))
    return wanted in window


def validate_report(report: AgentReport, tools: RepositoryTools) -> ValidationResult:
    """Check every finding against the repository the audit actually indexed."""
    known = tools.known_paths()
    accepted: list[AgentFinding] = []
    rejected: list[RejectedFinding] = []
    seen: set[tuple[str, str, int, int]] = set()

    for finding in report.findings:
        path = finding.file.replace("\\", "/").lstrip("./")
        if path not in known:
            rejected.append(
                RejectedFinding(finding.title, f"file {finding.file!r} is not in this repository")
            )
            continue

        lines = tools.file_lines(path)
        if lines is None:
            rejected.append(RejectedFinding(finding.title, f"file {path!r} could not be read"))
            continue
        if finding.line_start > len(lines):
            rejected.append(
                RejectedFinding(
                    finding.title,
                    f"line {finding.line_start} is past the end of {path} ({len(lines)} lines)",
                )
            )
            continue
        line_end = min(finding.line_end, len(lines))
        if finding.evidence_quote and not _quote_matches(
            finding.evidence_quote, lines, finding.line_start, line_end
        ):
            rejected.append(
                RejectedFinding(
                    finding.title,
                    f"the quoted evidence does not appear at {path}:{finding.line_start}-{line_end}",
                )
            )
            continue

        key = (finding.category, path, finding.line_start, line_end)
        if key in seen:
            rejected.append(RejectedFinding(finding.title, "duplicate of a finding in the same reply"))
            continue
        seen.add(key)
        accepted.append(finding.model_copy(update={"file": path, "line_end": line_end}))

    return ValidationResult(accepted=tuple(accepted), rejected=tuple(rejected))


def describe_errors(rejected: tuple[RejectedFinding, ...]) -> str:
    """A rejection note the model can act on in its single retry."""
    listed = "\n".join(f"- {item.render()}" for item in rejected[:10])
    return (
        "Your previous reply was rejected. Fix these and reply with the complete "
        f"JSON again:\n{listed}"
    )
