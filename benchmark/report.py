"""What repeated runs say, computed from stored rows and nothing else.

The harness scores one run at a time and prints it. That is the right output for
one run and the wrong one for a decision: a single recall figure from a
temperature-driven model is a sample, not a measurement. This module reads the
stored runs back and reports the spread - mean and min-max per configuration over
the last N runs - plus the two things a per-run table hides:

    per issue       how many of the N runs matched it, so an issue found once in
                    three is visibly different from one found every time
    authorization   the four planted access-control gaps, reported on their own,
                    because they are the only issues no scanner and no graph can
                    reach. Everything else in the manifest is scanner-visible or
                    cycle-visible, so this quartet is where the agent layer has
                    to earn its cost.

Nothing here writes, and nothing here recomputes a rate: every number comes from
``eval_runs.metrics`` and ``eval_results`` as stored. The rates are already in
``metrics["totals"]``; ``eval_results`` supplies which issue each run matched,
because a total cannot say which one.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from benchmark.harness import bootstrap

bootstrap.add_backend_to_path()

from sqlalchemy import text  # noqa: E402  (after sys.path is set up)
from sqlalchemy.orm import Session  # noqa: E402

from app.db.session import get_session_factory  # noqa: E402

CONFIG_ORDER = (
    "scanners_only",
    "single_agent_no_tools",
    "single_agent_tools",
    "multi_agent",
    "scanners_plus_deterministic",
)

#: The four planted access-control gaps: the only issues that need a model to
#: compare a handler against a helper. Everything else is visible to a scanner or
#: to the module graph.
AUTHORIZATION_ISSUES = (
    "adv-delete-authz-02",
    "adv-export-authz-01",
    "js-authz-01",
    "py-authz-01",
)

#: Configurations that spend model calls. Used only to average the quartet over
#: the configurations that could possibly find it.
AGENT_CONFIGS = ("single_agent_no_tools", "single_agent_tools", "multi_agent")

RATE_KEYS = ("recall", "precision", "cross_file_recall", "evidence_accuracy", "severity_accuracy")


@dataclass(frozen=True)
class RunMetrics:
    """One stored run, with the parts this report reads."""

    run_id: str
    config: str
    label: str | None
    started_at: datetime
    totals: dict[str, object]
    runtime_ms: int
    detected: frozenset[str]

    @property
    def secs(self) -> float:
        """Wall clock for the run's repositories, from the stored per-repo rows."""
        return self.runtime_ms / 1000

    @property
    def cost(self) -> Decimal:
        return Decimal(str(self.totals.get("cost_usd") or "0"))

    @property
    def false_positives(self) -> int:
        return int(self.totals.get("false_positives") or 0)

    def rate(self, key: str) -> float | None:
        """A stored rate, or ``None`` when the run measured nothing for it."""
        value = self.totals.get(key)
        return None if value is None else float(value)

    def issues(self) -> set[str]:
        """Every expected issue this run accounted for, matched or missed."""
        return set(self.totals.get("matched_ids") or ()) | set(self.totals.get("missed_ids") or ())


@dataclass(frozen=True)
class ConfigSummary:
    """One configuration's runs, in the order they ran."""

    config: str
    runs: tuple[RunMetrics, ...]

    def rate_values(self, key: str) -> list[float]:
        return [value for run in self.runs if (value := run.rate(key)) is not None]

    def costs(self) -> list[Decimal]:
        return [run.cost for run in self.runs]

    def secs(self) -> list[float]:
        return [run.secs for run in self.runs]

    def false_positives(self) -> list[int]:
        return [run.false_positives for run in self.runs]

    def matched(self, issue: str) -> int:
        """How many of these runs matched the issue."""
        return sum(1 for run in self.runs if issue in run.detected)

    def authorization_recall(self) -> float | None:
        """Matched authorization cells over every cell the runs could have got."""
        if not self.runs:
            return None
        cells = len(self.runs) * len(AUTHORIZATION_ISSUES)
        hits = sum(self.matched(issue) for issue in AUTHORIZATION_ISSUES)
        return round(hits / cells, 3)


def run_from_row(row, detected: set[str]) -> RunMetrics:
    """One ``eval_runs`` row in the shape this report reads."""
    stored = row["metrics"] or {}
    repos = stored.get("repos") or []
    return RunMetrics(
        run_id=str(row["id"]),
        config=str(row["config"]),
        label=stored.get("label"),
        started_at=row["started_at"],
        totals=stored.get("totals") or {},
        runtime_ms=sum(int(repo.get("runtime_ms") or 0) for repo in repos),
        detected=frozenset(detected),
    )


def _label_order(label: str) -> tuple[int, str]:
    """``repeat-2`` before ``repeat-10``."""
    return (len(label), label)


def latest_per_config(
    runs: list[RunMetrics], repeats: int = 3
) -> dict[str, ConfigSummary]:
    """One run per label of each configuration, oldest label first.

    Each configuration contributes its last ``repeats`` labels (``repeat-1`` ..
    ``repeat-3``). A label stored more than once — a batch that outlived the
    shell that launched it and was started again — contributes its earliest run
    only, so a duplicated repeat cannot be counted twice. Runs with no label
    fall back to the last ``repeats`` runs of the configuration.
    """
    grouped: dict[str, list[RunMetrics]] = defaultdict(list)
    for run in sorted(runs, key=lambda item: item.started_at):
        grouped[run.config].append(run)

    chosen: dict[str, tuple[RunMetrics, ...]] = {}
    for name, config_runs in grouped.items():
        by_label: dict[str, list[RunMetrics]] = defaultdict(list)
        for run in config_runs:
            if run.label:
                by_label[run.label].append(run)
        if by_label:
            labels = sorted(by_label, key=_label_order)[-repeats:]
            chosen[name] = tuple(by_label[label][0] for label in labels)
        else:
            chosen[name] = tuple(config_runs[-repeats:])

    order = [name for name in CONFIG_ORDER if name in grouped]
    order += [name for name in grouped if name not in CONFIG_ORDER]
    return {name: ConfigSummary(config=name, runs=chosen[name]) for name in order}


def load_runs(session: Session, *, repeats: int = 3) -> dict[str, ConfigSummary]:
    """Read every stored run and keep the last ``repeats`` of each configuration."""
    rows = session.execute(
        text("SELECT id, config, started_at, metrics FROM eval_runs ORDER BY started_at")
    ).mappings().all()
    detected: dict[str, set[str]] = defaultdict(set)
    for row in session.execute(
        text("SELECT eval_run_id, expected_issue_id FROM eval_results WHERE detected")
    ).mappings():
        detected[str(row["eval_run_id"])].add(str(row["expected_issue_id"]))

    runs = [run_from_row(row, detected.get(str(row["id"]), set())) for row in rows]
    return latest_per_config(runs, repeats)


def issues_measured(summaries: dict[str, ConfigSummary]) -> tuple[str, ...]:
    """Every expected issue any reported run accounted for, sorted by id."""
    found: set[str] = set()
    for summary in summaries.values():
        for run in summary.runs:
            found |= run.issues()
    return tuple(sorted(found))


def _cell(values: list, *, places: int = 3, money: bool = False, whole: bool = False) -> str:
    """Mean and min-max, or ``not recorded`` when there is nothing to average."""
    if not values:
        return "not recorded"
    mean = sum(values) / len(values)
    if money:
        return f"${mean:.4f} (${min(values):.4f}-${max(values):.4f})"
    if whole:
        return f"{mean:.2f} ({min(values)}-{max(values)})"
    return f"{mean:.{places}f} ({min(values):.{places}f}-{max(values):.{places}f})"


def _row(cells: tuple[str, ...], widths: tuple[int, ...]) -> str:
    return "  ".join(cell.ljust(width) for cell, width in zip(cells, widths))


def render(summaries: dict[str, ConfigSummary], issues: tuple[str, ...] | None = None) -> str:
    """The whole report: spread per configuration, then per issue, then the quartet."""
    issues = issues if issues is not None else issues_measured(summaries)
    repeats = max((len(summary.runs) for summary in summaries.values()), default=0)

    blocks: list[str] = [
        f"Repeated runs: the last {repeats} labelled run(s) of each configuration, "
        "from eval_runs and eval_results.",
        "",
        "Run ids",
    ]
    for name, summary in summaries.items():
        listed = "  ".join(
            f"{run.label or '-'}={run.run_id[:8]}" for run in summary.runs
        )
        blocks.append(f"  {name:<30} {listed}")
    blocks.append("")

    widths = (30, 21, 21, 14, 21, 21, 21, 27, 16)
    blocks.append(
        _row(
            ("configuration", "recall", "precision", "fp", "cross-file", "evidence", "severity", "cost", "secs"),
            widths,
        )
    )
    blocks.append(_row(tuple("-" * width for width in widths), widths))
    for name, summary in summaries.items():
        blocks.append(
            _row(
                (
                    name,
                    _cell(summary.rate_values("recall")),
                    _cell(summary.rate_values("precision")),
                    _cell(summary.false_positives(), whole=True),
                    _cell(summary.rate_values("cross_file_recall")),
                    _cell(summary.rate_values("evidence_accuracy")),
                    _cell(summary.rate_values("severity_accuracy")),
                    _cell(summary.costs(), money=True),
                    _cell([round(value) for value in summary.secs()], whole=True),
                ),
                widths,
            )
        )
    if not summaries:
        blocks.append("no stored runs: not recorded")

    blocks.extend(("", f"Expected issues: how many of the {repeats} run(s) matched each one"))
    issue_widths = (26, *([22] * len(summaries)))
    blocks.append(_row(("expected issue", *summaries.keys()), issue_widths))
    blocks.append(_row(tuple("-" * width for width in issue_widths), issue_widths))
    for issue in issues:
        if not any(issue in run.issues() for summary in summaries.values() for run in summary.runs):
            continue
        cells = [issue]
        for summary in summaries.values():
            cells.append(f"{summary.matched(issue)}/{len(summary.runs)}")
        blocks.append(_row(tuple(cells), issue_widths))

    blocks.extend(
        (
            "",
            "Agent-only recall on the four authorization issues "
            f"({', '.join(AUTHORIZATION_ISSUES)})",
        )
    )
    agent_means: list[float] = []
    for name, summary in summaries.items():
        value = summary.authorization_recall()
        cells = len(summary.runs) * len(AUTHORIZATION_ISSUES)
        hits = sum(summary.matched(issue) for issue in AUTHORIZATION_ISSUES)
        blocks.append(
            f"  {name:<30} {hits}/{cells} = {'n/a' if value is None else f'{value:.3f}'}"
        )
        if name in AGENT_CONFIGS and value is not None:
            agent_means.append(value)
    if agent_means:
        blocks.append(
            f"  {'agent configurations mean':<30} {sum(agent_means) / len(agent_means):.3f}"
        )
    return "\n".join(blocks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m benchmark.report",
        description="Report repeated benchmark runs from eval_runs, read-only.",
    )
    parser.add_argument(
        "--repeats", type=int, default=3, help="how many of the latest labels per configuration to use"
    )
    args = parser.parse_args(argv)

    session = get_session_factory()()
    try:
        summaries = load_runs(session, repeats=args.repeats)
    finally:
        session.close()
    print(render(summaries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
