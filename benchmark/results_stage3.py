"""Write benchmark/RESULTS.md from the stored stage-3 runs (step 3.11).

Read-only: every number comes from ``eval_runs.metrics``, ``eval_results`` and
``eval_extras`` as the harness stored them. Nothing is recomputed from finding
rows, so the file can be regenerated at any time and will either reproduce the
numbers or show that the stored runs changed.

    python benchmark/results_stage3.py [--prefix stage3-] [--out benchmark/RESULTS.md]
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from benchmark.harness import bootstrap

bootstrap.add_backend_to_path()

from sqlalchemy import text  # noqa: E402

from app.db.session import get_engine  # noqa: E402

HERE = Path(__file__).resolve().parent
EXPECTED_FILE = HERE / "expected.json"
DEFAULT_OUT = HERE / "RESULTS.md"

#: The four configurations the roadmap names, in the order it names them, and the
#: stored ``eval_config`` each one uses.
ROADMAP = (
    ("scanners_only", "scanners_only"),
    ("single_agent", "single_agent_no_tools"),
    ("multi_agent", "multi_agent"),
    ("multi_agent_validated", "multi_agent_validated"),
)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _f(value: float | None, places: int = 3) -> str:
    return "not recorded" if value is None else f"{value:.{places}f}"


def _money(value: float | None) -> str:
    return "not recorded" if value is None else f"${value:.4f}"


def _spread(values: list[float], places: int = 3) -> str:
    if not values:
        return "not recorded"
    return f"{min(values):.{places}f} – {max(values):.{places}f}"


def _issues_by_id() -> dict[str, dict]:
    return {issue["id"]: issue for issue in json.loads(EXPECTED_FILE.read_text(encoding="utf-8"))["issues"]}


def load(prefix: str) -> dict:
    engine = get_engine()
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, config, started_at, metrics FROM eval_runs "
                "WHERE metrics->>'label' LIKE :pattern ORDER BY started_at"
            ),
            {"pattern": f"{prefix}%"},
        ).mappings().all()
        runs: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            metrics = row["metrics"] or {}
            totals = metrics.get("totals") or {}
            runs[str(row["config"])].append(
                {
                    "id": str(row["id"]),
                    "label": metrics.get("label"),
                    "started_at": row["started_at"],
                    "metrics": metrics,
                    "totals": totals,
                    "repos": metrics.get("repos") or [],
                }
            )

        run_ids = [run["id"] for group in runs.values() for run in group]
        details: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
        if run_ids:
            for row in connection.execute(
                text(
                    "SELECT eval_run_id, repo_name, expected_issue_id, detected, miss_reason "
                    "FROM eval_results WHERE eval_run_id::text = ANY(:ids) "
                    "ORDER BY repo_name, expected_issue_id"
                ),
                {"ids": run_ids},
            ).mappings():
                details[str(row["eval_run_id"])][str(row["expected_issue_id"])].append(dict(row))
    return {"runs": runs, "details": details}


def summarise(runs: list[dict]) -> dict:
    totals = [run["totals"] for run in runs]
    per_repo_costs: list[float] = []
    for run in runs:
        for repo in run["repos"]:
            per_repo_costs.append(float(repo.get("cost_usd") or 0))
    repo_count = sum(len(run["repos"]) for run in runs) or 1
    return {
        "recall": _mean([float(item["recall"]) for item in totals if item.get("recall") is not None]),
        "precision": _mean([float(item["precision"]) for item in totals if item.get("precision") is not None]),
        "fp_rate": _mean(
            [float(item["false_positive_rate"]) for item in totals if item.get("false_positive_rate") is not None]
        ),
        "evidence": _mean(
            [float(item["evidence_accuracy"]) for item in totals if item.get("evidence_accuracy") is not None]
        ),
        "severity": _mean(
            [float(item["severity_accuracy"]) for item in totals if item.get("severity_accuracy") is not None]
        ),
        "cross_file": _mean(
            [float(item["cross_file_recall"]) for item in totals if item.get("cross_file_recall") is not None]
        ),
        "cost_total": _mean([float(item.get("cost_usd") or 0) for item in totals]),
        "cost_per_repo": _mean(per_repo_costs),
        "runtime_s": _mean([float(item.get("runtime_ms") or 0) / 1000 for item in totals]),
        "matched": [int(item.get("matched") or 0) for item in totals],
        "recall_runs": [float(item["recall"]) for item in totals if item.get("recall") is not None],
        "precision_runs": [float(item["precision"]) for item in totals if item.get("precision") is not None],
        "fp_runs": [
            float(item["false_positive_rate"])
            for item in totals
            if item.get("false_positive_rate") is not None
        ],
        "evidence_runs": [
            float(item["evidence_accuracy"])
            for item in totals
            if item.get("evidence_accuracy") is not None
        ],
        "severity_runs": [
            float(item["severity_accuracy"])
            for item in totals
            if item.get("severity_accuracy") is not None
        ],
        "cross_file_runs": [
            float(item["cross_file_recall"])
            for item in totals
            if item.get("cross_file_recall") is not None
        ],
        "runtime_runs": [float(item.get("runtime_ms") or 0) / 1000 for item in totals],
        "cost_runs": [float(item.get("cost_usd") or 0) for item in totals],
    }


def collect_issues(runs: list[dict], details: dict) -> tuple[dict[str, dict], dict[str, dict]]:
    """Missed planted issues and false positives, with a count out of the runs."""
    issues = _issues_by_id()
    missed: dict[str, dict] = {}
    extras: dict[str, dict] = {}
    engine = get_engine()
    run_ids = [run["id"] for run in runs]
    with engine.connect() as connection:
        for row in connection.execute(
            text(
                "SELECT eval_run_id, repo, category, title, file, line_start, source_tool "
                "FROM eval_extras WHERE eval_run_id::text = ANY(:ids)"
            ),
            {"ids": run_ids},
        ).mappings():
            key = f"{row['repo']}|{row['category']}|{row['title']}|{row['file']}"
            entry = extras.setdefault(
                key,
                {
                    "repo": row["repo"],
                    "category": row["category"],
                    "title": row["title"],
                    "file": row["file"],
                    "line_start": row["line_start"],
                    "source_tool": row["source_tool"],
                    "runs": set(),
                },
            )
            entry["runs"].add(str(row["eval_run_id"]))
        for run in runs:
            for issue_id, rows in details.get(run["id"], {}).items():
                if any(row["detected"] for row in rows):
                    continue
                issue = issues.get(issue_id, {"id": issue_id, "repo": rows[0]["repo_name"]})
                entry = missed.setdefault(
                    issue_id,
                    {
                        "id": issue_id,
                        "repo": rows[0]["repo_name"],
                        "category": issue.get("category"),
                        "title": issue.get("title"),
                        "file": issue.get("file"),
                        "line_start": issue.get("line_start"),
                        "cross_file": issue.get("cross_file", False),
                        "runs": set(),
                    },
                )
                entry["runs"].add(run["id"])
    return missed, extras


def render(prefix: str) -> str:
    loaded = load(prefix)
    runs = loaded["runs"]
    missing = [name for name, stored in ROADMAP if len(runs.get(stored, [])) != 3]
    if not runs:
        raise SystemExit(f"no stored runs with label prefix {prefix!r}")

    lines: list[str] = []
    add = lines.append
    add("# Stage 3 benchmark results")
    add("")
    add(
        f"Benchmark version {json.loads(EXPECTED_FILE.read_text(encoding='utf-8')).get('benchmark_version')}. "
        "Four configurations, three runs each, against the four-repository fixture set "
        "(`clean_app`, `js_app`, `python_app`, `adversarial_app`; 18 planted issues)."
    )
    add("")
    add(
        "Each run was produced from the repository root with "
        "`python -m benchmark.harness --config <configuration> --label "
        f"{prefix}<configuration>-<n>`, and read back with `python benchmark/results_stage3.py`. "
        "All numbers below are stored values: totals come from `eval_runs.metrics` and the "
        "per-issue detail from `eval_results` and `eval_extras`."
    )
    add("")
    if missing:
        add(
            "> Run count is not three for: "
            + ", ".join(f"`{name}`" for name in missing)
            + ". Figures below cover the runs that exist."
        )
        add("")

    add("## Configurations")
    add("")
    add("| roadmap name | stored `eval_config` | stages | uses a model |")
    add("| --- | --- | --- | --- |")
    for name, stored in ROADMAP:
        group = runs.get(stored, [])
        metrics = group[0]["metrics"] if group else {}
        stages = ", ".join(metrics.get("stages") or []) or "—"
        limits = metrics.get("limits") or {}
        add(f"| {name} | `{stored}` | {stages} | {'yes' if limits.get('calls_model') else 'no'} |")
    add("")

    add("## Average metrics per configuration")
    add("")
    add(
        "Means over the three runs. Cost per repository is the mean of the per-repository "
        "`cost_usd` stored in each run; runtime is the sum of the run's repository wall times."
    )
    add("")
    add(
        "| configuration | runs | precision | recall | false-positive rate | evidence accuracy | "
        "severity accuracy | cross-file recall | cost per repository | runtime (s) | reproducibility spread (recall) |"
    )
    add("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
    summaries = {}
    for name, stored in ROADMAP:
        group = runs.get(stored, [])
        summary = summarise(group) if group else {}
        summaries[name] = summary
        add(
            "| {name} | {count} | {precision} | {recall} | {fp} | {evidence} | {severity} | "
            "{cross} | {cost} | {runtime} | {spread} |".format(
                name=name,
                count=len(group),
                precision=_f(summary.get("precision")),
                recall=_f(summary.get("recall")),
                fp=_f(summary.get("fp_rate")),
                evidence=_f(summary.get("evidence")),
                severity=_f(summary.get("severity")),
                cross=_f(summary.get("cross_file")),
                cost=_money(summary.get("cost_per_repo")),
                runtime=_f(summary.get("runtime_s"), 1),
                spread=_spread(summary.get("recall_runs", [])),
            )
        )
    add("")

    add("## Reproducibility spread")
    add("")
    add(
        "Per-run values for the same configuration. A zero-width range means the three runs "
        "produced the same value."
    )
    add("")
    add("| configuration | metric | run 1 | run 2 | run 3 | range |")
    add("| --- | --- | ---: | ---: | ---: | --- |")
    for name, stored in ROADMAP:
        summary = summaries.get(name) or {}
        for key, label, places in (
            ("recall_runs", "recall", 3),
            ("precision_runs", "precision", 3),
            ("fp_runs", "false-positive rate", 3),
            ("evidence_runs", "evidence accuracy", 3),
            ("severity_runs", "severity accuracy", 3),
            ("cross_file_runs", "cross-file recall", 3),
            ("runtime_runs", "runtime (s)", 1),
            ("cost_runs", "cost per run (USD)", 4),
        ):
            values = summary.get(key) or []
            if not values:
                continue
            cells = " | ".join(f"{value:.{places}f}" for value in values)
            add(f"| {name} | {label} | {cells} | {_spread(values, places)} |")
        matched = summary.get("matched") or []
        if matched:
            add(f"| {name} | matched issues | {' | '.join(str(value) for value in matched)} | {min(matched)} – {max(matched)} |")
    add("")

    add("## Missed planted issues")
    add("")
    add("Union of misses across the three runs of a configuration. `0/3` means every run missed it.")
    add("")
    for name, stored in ROADMAP:
        group = runs.get(stored, [])
        missed, _ = collect_issues(group, loaded["details"]) if group else ({}, {})
        add(f"### {name}")
        add("")
        if not missed:
            add("No planted issue was missed in any of the runs.")
            add("")
            continue
        add("| expected issue | repo | category | cross-file | location | missed in runs |")
        add("| --- | --- | --- | --- | --- | ---: |")
        for entry in sorted(missed.values(), key=lambda item: (item["repo"], item["id"])):
            location = entry.get("file") or "—"
            if entry.get("line_start"):
                location += f":{entry['line_start']}"
            title = entry.get("title") or entry["id"]
            add(
                f"| `{entry['id']}` {title} | {entry['repo']} | {entry.get('category') or '—'} | "
                f"{'yes' if entry.get('cross_file') else 'no'} | `{location}` | "
                f"{len(entry['runs'])}/3 |"
            )
        add("")

    add("## False positives")
    add("")
    add(
        "Findings stored by a configuration that matched no planted issue. The clean repository "
        "(`clean_app`) has no expected issues, so every finding reported there is listed here."
    )
    add("")
    for name, stored in ROADMAP:
        group = runs.get(stored, [])
        _, extras = collect_issues(group, loaded["details"]) if group else ({}, {})
        add(f"### {name}")
        add("")
        if not extras:
            add("No unmatched findings were stored in any of the runs.")
            add("")
            continue
        add("| repo | category | title | location | source | runs reported |")
        add("| --- | --- | --- | --- | --- | ---: |")
        for entry in sorted(extras.values(), key=lambda item: (item["repo"], item["title"] or "")):
            location = entry.get("file") or "—"
            if entry.get("line_start"):
                location += f":{entry['line_start']}"
            add(
                f"| {entry['repo']} | {entry.get('category') or '—'} | "
                f"{(entry.get('title') or '').replace('|', '/')[:120]} | `{location}` | "
                f"{entry.get('source_tool') or '—'} | {len(entry['runs'])}/3 |"
            )
        add("")

    add("## CrewAI usage")
    add("")
    add(
        "`multi_agent` and `multi_agent_validated` run their agent stage through CrewAI. "
        "In `backend/app/agents/security.py`, `architecture.py` and `compliance.py` each "
        "finding-producing stage builds `crewai.Agent` / `Crew` / `Task` objects "
        "(`Crew(agents=[reviewer], tasks=[review], process=Process.sequential)`), and "
        "`backend/app/agents/crew.py` adapts the audit's budgeted LLM client to "
        "`crewai.llms.base_llm.BaseLLM` so every crew call is charged to the audit. "
        "`multi_agent_validated` adds the validation pass in `backend/app/validation/validator.py`; "
        "that pass is plain Python plus one model call per agent finding, not a CrewAI crew. "
        "`single_agent` (`single_agent_no_tools`) does not use CrewAI: it asks the model once "
        "in `backend/app/agents/baseline_single.py`. `scanners_only` makes no model call."
    )
    add("")
    add(
        "A plain-LangGraph variant of the multi-agent stage (the roadmap's fallback if CrewAI "
        "shows no measurable gain) has not been run in this benchmark. No numbers for it are "
        "recorded here; the configurations above are the only ones measured."
    )
    add("")

    add("## Run records")
    add("")
    add("| configuration | label | eval_run id | started (UTC) | models | budget per repository |")
    add("| --- | --- | --- | --- | --- | --- |")
    for name, stored in ROADMAP:
        for run in runs.get(stored, []):
            metrics = run["metrics"]
            models = metrics.get("models") or []
            model_text = ", ".join(str(model) for model in models) if models else "no model call"
            budget = (metrics.get("limits") or {}).get("budget_usd")
            add(
                f"| {name} | `{run['label']}` | `{run['id']}` | "
                f"{run['started_at'].strftime('%Y-%m-%d %H:%M')} | {model_text} | "
                f"{'—' if budget is None else '$' + str(budget)} |"
            )
    add("")

    add("## Notes on method")
    add("")
    add(
        "- Matching is by category, file and overlapping line range, one-to-one, as implemented "
        "in `benchmark/harness/matching.py`."
    )
    add(
        "- Precision counts distinct reported issues; repeat findings for the same issue are "
        "excluded from its denominator and reported as duplicates in `eval_runs.metrics`."
    )
    add(
        "- Evidence accuracy and severity accuracy are computed over matched issues only. "
        "A configuration that matched nothing therefore has no value for them rather than 1.0."
    )
    add(
        "- Cross-file recall covers the six planted issues marked `cross_file: true` in "
        "`benchmark/expected.json`."
    )
    add(
        "- Cost is the sum of stored `agent_runs` spend for the run, in USD, as recorded when "
        "the call was made."
    )
    add("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", default="stage3-", help="eval_runs label prefix")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="markdown file to write")
    args = parser.parse_args(argv)
    output = render(args.prefix)
    Path(args.out).write_text(output + "\n", encoding="utf-8")
    print(f"wrote {args.out} ({len(output.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
