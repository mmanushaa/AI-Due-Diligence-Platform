"""The printed table, and the arithmetic behind three of its numbers.

The table is narrow on purpose: one row per repository plus a total, every rate as
a decimal, and next to it the division it came from. The arithmetic section then
walks three aggregates line by line — recall, precision and the false-positive
rate — so a reader can check them with a calculator instead of trusting the table.

Rates with no denominator print as ``n/a``: a repository with nothing planted
measures nothing, and showing ``0.000`` there would read as a failure.
"""

from __future__ import annotations

from decimal import Decimal

from benchmark.harness.metrics import RepoScore

COLUMNS = (
    ("repo", 20),
    ("exp", 4),
    ("found", 5),
    ("match", 5),
    ("dup", 4),
    ("recall", 7),
    ("prec", 7),
    ("fpr", 7),
    ("evid", 7),
    ("sev", 7),
    ("xfile", 7),
    ("cost", 9),
    ("secs", 7),
)


def _rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _money(value: Decimal) -> str:
    return f"${value:.4f}"


def _row(cells: tuple[str, ...]) -> str:
    return "  ".join(cell.ljust(width) for cell, (_, width) in zip(cells, COLUMNS))


def render_table(scores: tuple[RepoScore, ...], total: RepoScore) -> str:
    """The per-repository table plus the total row."""
    lines = [
        _row(tuple(name for name, _ in COLUMNS)),
        _row(tuple("-" * width for _, width in COLUMNS)),
    ]
    for score in (*scores, total):
        lines.append(
            _row(
                (
                    score.repo,
                    str(score.expected),
                    str(score.findings),
                    str(score.matched),
                    str(score.duplicates),
                    _rate(score.recall),
                    _rate(score.precision),
                    _rate(score.false_positive_rate),
                    _rate(score.evidence_accuracy),
                    _rate(score.severity_accuracy),
                    _rate(score.cross_file_recall),
                    _money(score.cost_usd),
                    f"{score.runtime_ms / 1000:.1f}",
                )
            )
        )
    return "\n".join(lines)


def render_legend() -> str:
    """What each column means, in one screen."""
    return "\n".join(
        (
            "  exp     expected issues in the manifest for that repository",
            "  found   findings stored by the audit",
            "  match   expected issues reported at the right file and overlapping lines, same category",
            "  dup     findings that repeat an already-matched issue (one row per advisory)",
            "  recall  match / exp",
            "  prec    match / (match + false positives)",
            "  fpr     false positives / (match + false positives)",
            "  evid    cited lines hold the claimed thing / match",
            "  sev     reported severity equals the expected one / match",
            "  xfile   cross-file issues found / cross-file issues planted",
            "  cost    model spend for that repository, from agent_runs",
            "  secs    wall clock for that repository's whole run, indexing included",
        )
    )


def _division(label: str, numerator: int, denominator: int, value: float | None) -> str:
    if denominator == 0:
        return f"{label}: n/a (nothing to divide)"
    if value is None:  # pragma: no cover - denominator > 0 always yields a value
        return f"{label}: n/a"
    return f"{label} = {numerator} / {denominator} = {value:.4f}"


def render_arithmetic(total: RepoScore) -> str:
    """The three headline numbers written out as a division."""
    lines = [
        "Arithmetic behind the totals",
        f"  matched {total.matched} of {total.expected} expected; "
        f"reported {total.findings} findings.",
        "",
        "  1. recall",
        f"     {_division('recall', total.matched, total.expected, total.recall)}",
        f"     matched: {', '.join(total.matched_ids) or '-'}",
        f"     missed:  {', '.join(total.missed_ids) or '-'}",
        "",
        "  2. precision",
        f"     distinct reported issues = matched + false positives = "
        f"{total.matched} + {total.false_positive_count} = {total.distinct_reported}",
        f"     {_division('precision', total.matched, total.distinct_reported, total.precision)}",
        f"     {total.duplicates} further finding(s) repeat an issue that is already counted",
        "     (one row per advisory per dependency) and are excluded from this denominator.",
        "",
        "  3. false-positive rate",
        f"     {_division('false-positive rate', total.false_positive_count, total.distinct_reported, total.false_positive_rate)}",
    ]
    if total.false_positives:
        lines.append("     unmatched findings:")
        lines.extend(f"       - {item}" for item in total.false_positives)
    else:
        lines.append("     no false positives: every distinct report matched a planted issue")
    return "\n".join(lines)


def render_scanner_warnings(evaluations) -> str:
    """Warn when a scanner failed: a dead tool looks like a miss, not an error."""
    lines: list[str] = []
    for evaluation in evaluations:
        failed = {
            tool: status
            for tool, status in evaluation.scanner_status.items()
            if status not in ("succeeded",)
        }
        if failed:
            detail = ", ".join(f"{tool}={status}" for tool, status in sorted(failed.items()))
            lines.append(f"  ! {evaluation.score.repo}: {detail}")
    if not lines:
        return ""
    return "\n".join(("Scanner warnings (recall for these tools is not meaningful):", *lines))


def render_agent_summary(evaluations) -> str:
    """One line per repository that ran an agent stage."""
    lines: list[str] = []
    for evaluation in evaluations:
        outcome = evaluation.agent_outcome
        if not outcome:
            continue
        line = (
            f"  {evaluation.score.repo:<20} model={outcome.get('model') or '-'} "
            f"calls={outcome.get('calls')} accepted={outcome.get('accepted')} "
            f"rejected={outcome.get('rejected')} cost=${outcome.get('cost_usd')}"
        )
        if outcome.get("triaged") is not None:
            line += (
                f" triaged={outcome.get('triaged')} real={outcome.get('likely_real')}"
                f" false_positive={outcome.get('likely_false_positive')}"
                f" uncertain={outcome.get('uncertain')}"
            )
        if outcome.get("latency_ms"):
            line += f" {int(outcome['latency_ms']) / 1000:.0f}s"
        lines.append(line)
        for note in outcome.get("notes") or ():
            lines.append(f"    . {note}")
    return "\n".join(("Agent stage:", *lines)) if lines else ""


def render_agent_warnings(evaluations) -> str:
    """An agent that could not call the model looks like a miss, so say so."""
    lines: list[str] = []
    for evaluation in evaluations:
        outcome = evaluation.agent_outcome or {}
        errors = outcome.get("errors") or ()
        if errors:
            detail = "; ".join(str(error) for error in errors)[:400]
            lines.append(f"  ! {evaluation.score.repo}: {detail}")
    if not lines:
        return ""
    return "\n".join(("Agent warnings (recall for the agent layer is not meaningful):", *lines))


def render(record) -> str:
    """Everything the harness prints for one run."""
    blocks = [
        f"config      {record.configuration.name} - {record.configuration.summary}",
        f"benchmark   {record.benchmark_version}",
        f"eval_run    {record.eval_run_id or '(not saved)'}",
        "",
        render_table(record.scores, record.total),
        "",
        render_legend(),
        "",
        render_arithmetic(record.total),
    ]
    agent_summary = render_agent_summary(record.evaluations)
    if agent_summary:
        blocks.extend(("", agent_summary))
    for warnings in (
        render_scanner_warnings(record.evaluations),
        render_agent_warnings(record.evaluations),
    ):
        if warnings:
            blocks.extend(("", warnings))
    return "\n".join(blocks)
