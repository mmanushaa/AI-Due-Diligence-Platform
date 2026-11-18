"""Write benchmark/RESULTS_VALIDATOR.md from the stored validator runs (step 2.10b).

Read-only: every number comes from ``eval_runs.metrics``, ``eval_results``,
``eval_extras``, ``findings``, ``evidence``, ``finding_events``,
``validation_checks`` and ``agent_runs`` as the harness and the validator stored
them. Nothing is recomputed from a fixture and no row is written or edited, so the
file can be regenerated at any time and will either reproduce the numbers or show
that the stored runs changed.

    python -m benchmark.results_validator [--out benchmark/RESULTS_VALIDATOR.md]

The groups this report contrasts:

    repeat-1..3                 whatever configuration each label was stored with.
                                The label was reused on 2026-11-10, so a label can
                                name more than one run and each is listed.
    stage3-multi_agent-1..3     the clean three-run ``multi_agent`` baseline.
    valid-1..3                  ``multi_agent_validated`` before the strict verdict
                                contract.
    val2-1..3                   ``multi_agent_validated`` with the strict contract.

A run's audit ids are taken from the rows that name them: the validation checks the
harness linked, and the findings its ``eval_results``/``eval_extras`` rows point at.
When those do not cover all four fixture repositories (the pre-contract runs have no
check rows, and a repository may store no matching finding), the audits of that
configuration created between this run's start and the next run's start are added;
the report says when that fallback was used.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from benchmark.harness import bootstrap

bootstrap.add_backend_to_path()

from sqlalchemy import text  # noqa: E402

from app.db.session import get_engine  # noqa: E402
from benchmark.harness import matching  # noqa: E402
from benchmark.harness.expected import ManifestError, load_manifest  # noqa: E402

HERE = Path(__file__).resolve().parent
EXPECTED_FILE = HERE / "expected.json"
REPOS_ROOT = HERE / "repos"
DEFAULT_OUT = HERE / "RESULTS_VALIDATOR.md"

#: Group name and the stored labels it selects, in report order.
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("repeat-* (labels as stored)", ("repeat-1", "repeat-2", "repeat-3")),
    (
        "multi_agent baseline",
        ("stage3-multi_agent-1", "stage3-multi_agent-2", "stage3-multi_agent-3"),
    ),
    ("valid (pre-contract)", ("valid-1", "valid-2", "valid-3")),
    ("val2 (strict contract)", ("val2-1", "val2-2", "val2-3")),
)

#: Labels that were reused for different configurations, so a time window cannot say
#: which audits belong to which row; only the row's own exact links are trusted.
AMBIGUOUS_LABELS = frozenset(("repeat-1", "repeat-2", "repeat-3"))

VERDICTS = ("supported", "contradicted", "cannot_tell")
STATUSES = ("verified", "rejected_false_positive", "unresolved")
RAW_LIMIT = 320


@dataclass
class Run:
    """One stored ``eval_runs`` row plus what was read back for it."""

    id: str
    config: str
    label: str
    started_at: datetime | None
    metrics: dict
    window_end: datetime | None = None
    audits: list[str] = field(default_factory=list)
    audit_source: str = "rows"
    statuses: Counter = field(default_factory=Counter)
    checks: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    agent_failures: list[dict] = field(default_factory=list)
    validator_failures: list[dict] = field(default_factory=list)
    unlinked_checks: int = 0
    rejected_detected: int = 0
    result_rows: int = 0
    orphan_matched_ids: int = 0

    @property
    def totals(self) -> dict:
        return (self.metrics or {}).get("totals") or {}


def _f(value, places: int = 3) -> str:
    if value is None:
        return "not recorded"
    return f"{float(value):.{places}f}"


def _money(value) -> str:
    if value is None:
        return "not recorded"
    return f"${float(value):.4f}"


def _cell(value, limit: int = 0) -> str:
    """A markdown-safe single-line cell."""
    if value is None:
        return "—"
    text_value = " ".join(str(value).split()).replace("|", "/")
    if limit and len(text_value) > limit:
        return text_value[: limit - 1] + "…"
    return text_value


def _load_runs(connection) -> tuple[list[Run], list[str]]:
    rows = connection.execute(
        text(
            "SELECT id, config::text AS config, started_at, metrics "
            "FROM eval_runs ORDER BY started_at"
        )
    ).mappings().all()
    runs: list[Run] = []
    for row in rows:
        label = (row["metrics"] or {}).get("label")
        runs.append(
            Run(
                id=str(row["id"]),
                config=row["config"],
                label=str(label) if label else "",
                started_at=row["started_at"],
                metrics=row["metrics"] or {},
            )
        )
    # The fallback window ends at the next stored run's start, so it only covers
    # audits one invocation could have created.
    for index, run in enumerate(runs):
        run.window_end = runs[index + 1].started_at if index + 1 < len(runs) else None

    stored = {run.label for run in runs if run.label}
    missing = [label for _, labels in GROUPS for label in labels if label not in stored]
    return runs, missing


def _read_audits(connection, run: Run) -> None:
    ids: set[str] = set()
    for sql in (
        "SELECT DISTINCT audit_id::text FROM validation_checks WHERE eval_run_id = :run",
        "SELECT DISTINCT f.audit_id::text FROM eval_results r "
        "JOIN findings f ON f.id = r.matched_finding_id WHERE r.eval_run_id = :run",
        "SELECT DISTINCT f.audit_id::text FROM eval_extras e "
        "JOIN findings f ON f.id = e.finding_id WHERE e.eval_run_id = :run",
    ):
        ids |= {row[0] for row in connection.execute(text(sql), {"run": run.id})}
    linked = len(ids)
    if linked < 4 and run.label not in AMBIGUOUS_LABELS:
        rows = connection.execute(
            text(
                "SELECT a.id::text FROM audits a "
                "JOIN repositories rp ON rp.id = a.repository_id "
                "WHERE a.config->>'eval_config' = :config "
                "AND a.created_at >= :start "
                "AND (:end IS NULL OR a.created_at < :end)"
            ),
            {"config": run.config, "start": run.started_at, "end": run.window_end},
        )
        ids |= {row[0] for row in rows}
        run.audit_source = "rows+window" if linked else "window"
    elif linked < 4:
        run.audit_source = "rows (ambiguous label)"
    run.audits = sorted(ids)


def _read_statuses(connection, run: Run) -> None:
    if not run.audits:
        return
    rows = connection.execute(
        text(
            "SELECT verification_status::text AS status, count(*) AS n "
            "FROM findings WHERE audit_id::text = ANY(:ids) GROUP BY 1"
        ),
        {"ids": run.audits},
    )
    run.statuses = Counter({row[0]: int(row[1]) for row in rows})


def _read_checks(connection, run: Run) -> None:
    run.checks = [
        dict(row)
        for row in connection.execute(
            text(
                "SELECT finding_id::text AS finding_id, verdict, quote, raw_reply, "
                "cited_file, line_start, line_end, latency_ms, cost_usd "
                "FROM validation_checks WHERE eval_run_id = :run ORDER BY created_at"
            ),
            {"run": run.id},
        ).mappings()
    ]
    if not run.audits:
        return
    run.unlinked_checks = int(
        connection.execute(
            text(
                "SELECT count(*) FROM validation_checks "
                "WHERE eval_run_id IS NULL AND audit_id::text = ANY(:ids)"
            ),
            {"ids": run.audits},
        ).scalar_one()
    )


def _read_rejected(connection, run: Run, manifest) -> None:
    if not run.audits:
        return
    rows = connection.execute(
        text(
            "SELECT f.id::text AS finding_id, f.title, f.reasoning, "
            "f.category::text AS category, f.severity::text AS severity, "
            "rp.source_ref AS repo "
            "FROM findings f "
            "JOIN audits a ON a.id = f.audit_id "
            "JOIN repositories rp ON rp.id = a.repository_id "
            "WHERE f.audit_id::text = ANY(:ids) "
            "AND f.verification_status::text = 'rejected_false_positive' "
            "ORDER BY rp.source_ref, f.category, f.title"
        ),
        {"ids": run.audits},
    ).mappings().all()
    findings = [dict(row) for row in rows]
    if not findings:
        return

    ids = [item["finding_id"] for item in findings]
    spans: dict[str, list[matching.Span]] = {}
    for row in connection.execute(
        text(
            "SELECT finding_id::text AS finding_id, file_path, line_start, line_end "
            "FROM evidence WHERE finding_id::text = ANY(:ids) ORDER BY line_start"
        ),
        {"ids": ids},
    ).mappings():
        spans.setdefault(row["finding_id"], []).append(
            matching.Span(
                file_path=row["file_path"],
                line_start=int(row["line_start"]),
                line_end=int(row["line_end"]),
            )
        )

    checks = {item["finding_id"]: item for item in run.checks}
    notes = {
        row["finding_id"]: row["note"]
        for row in connection.execute(
            text(
                "SELECT DISTINCT ON (finding_id) finding_id::text AS finding_id, note "
                "FROM finding_events WHERE finding_id::text = ANY(:ids) "
                "AND to_status::text = 'rejected_false_positive' "
                "ORDER BY finding_id, created_at DESC"
            ),
            {"ids": ids},
        ).mappings()
    }

    for item in findings:
        evidence = tuple(spans.get(item["finding_id"], ()))
        reported = matching.ReportedFinding(
            category=item["category"],
            severity=item["severity"],
            evidence=evidence,
            title=item["title"] or "",
            reasoning=item["reasoning"] or "",
        )
        issues = manifest.for_repo(item["repo"]) if item["repo"] else ()
        item["matched_issues"] = [issue.id for issue in issues if matching.matches(issue, reported)]
        item["evidence"] = evidence
        item["check"] = checks.get(item["finding_id"])
        item["note"] = notes.get(item["finding_id"])
    run.rejected = findings


def _read_calls(connection, run: Run) -> None:
    if not run.audits:
        return
    run.agent_failures = [
        dict(row)
        for row in connection.execute(
            text(
                "SELECT agent::text AS agent, model, status, count(*) AS n "
                "FROM agent_runs WHERE audit_id::text = ANY(:ids) "
                "AND status IS DISTINCT FROM 'succeeded' GROUP BY 1, 2, 3 ORDER BY 1, 2"
            ),
            {"ids": run.audits},
        ).mappings()
    ]
    run.validator_failures = [
        dict(row)
        for row in connection.execute(
            text(
                "SELECT f.id::text AS finding_id, f.title, e.note "
                "FROM finding_events e JOIN findings f ON f.id = e.finding_id "
                "WHERE f.audit_id::text = ANY(:ids) AND e.actor = 'validator' "
                "AND e.note LIKE 'model check failed%' ORDER BY f.id"
            ),
            {"ids": run.audits},
        ).mappings()
    ]
    run.rejected_detected = int(
        connection.execute(
            text(
                "SELECT count(*) FROM eval_results r "
                "JOIN findings f ON f.id = r.matched_finding_id "
                "WHERE r.eval_run_id = :run AND r.detected "
                "AND f.verification_status::text = 'rejected_false_positive'"
            ),
            {"run": run.id},
        ).scalar_one()
    )


def _read_results(connection, run: Run) -> None:
    """The scored rows the run stored, and any matched id that escaped the run."""
    run.result_rows = int(
        connection.execute(
            text("SELECT count(*) FROM eval_results WHERE eval_run_id = :run"),
            {"run": run.id},
        ).scalar_one()
    )
    run.orphan_matched_ids = int(
        connection.execute(
            text(
                "SELECT count(*) FROM eval_results r "
                "LEFT JOIN findings f ON f.id = r.matched_finding_id "
                "WHERE r.eval_run_id = :run AND r.matched_finding_id IS NOT NULL "
                "AND (f.id IS NULL OR f.audit_id NOT IN "
                "  (SELECT id FROM audits WHERE eval_run_id = :run))"
            ),
            {"run": run.id},
        ).scalar_one()
    )


def load():
    engine = get_engine()
    manifest = load_manifest(EXPECTED_FILE, REPOS_ROOT)
    with engine.connect() as connection:
        runs, missing = _load_runs(connection)
        for run in runs:
            if not any(run.label == label for _, labels in GROUPS for label in labels):
                continue
            _read_audits(connection, run)
            _read_statuses(connection, run)
            _read_checks(connection, run)
            _read_rejected(connection, run, manifest)
            _read_calls(connection, run)
            _read_results(connection, run)
    return runs, missing, manifest


def _selected(runs: list[Run], labels: tuple[str, ...]) -> list[Run]:
    return [run for run in runs if run.label in labels]


def _stored_validation(run: Run) -> dict:
    """The validator's own per-repository counts, summed from ``metrics``."""
    totals = Counter()
    repositories = 0
    for block in ((run.metrics or {}).get("agents") or {}).values():
        validation = (block or {}).get("validation") if isinstance(block, dict) else None
        if not isinstance(validation, dict):
            continue
        repositories += 1
        for key in ("checked", "verified", "rejected", "unresolved", "calls"):
            totals[key] += int(validation.get(key) or 0)
        totals["cost_usd"] = Decimal(totals["cost_usd"]) + Decimal(
            str(validation.get("cost_usd") or "0")
        )
    totals["repositories"] = repositories
    return totals


def render(runs: list[Run], missing: list[str]) -> str:
    if not any(run.label for run in runs):
        raise SystemExit("no stored runs for repeat-*/stage3-multi_agent-*/valid-*/val2-*")

    lines: list[str] = []
    add = lines.append
    add("# Validator contract results (step 2.10b)")
    add("")
    add(
        "Every number below is read back from stored rows: `eval_runs.metrics` for the "
        "scores, and `findings`, `evidence`, `validation_checks`, `finding_events`, "
        "`agent_runs`, `eval_results` and `eval_extras` for the per-finding detail. No row "
        "was inserted or edited by hand; the runs were produced by "
        "`python -m benchmark.harness --config <configuration> --label <label>`."
    )
    add("")
    add(
        "`val2-1..3` are `multi_agent_validated` with the strict verdict contract "
        "(`supported` | `contradicted` | `cannot_tell` plus a quote copied from the cited "
        "code). `valid-1..3` are the same configuration before that contract, and "
        "`stage3-multi_agent-1..3` are `multi_agent` with no validation pass at all. The "
        "`repeat-*` labels were reused across configurations on 2026-11-10, so that group "
        "lists every stored row under those labels and says which configuration each one "
        "actually ran."
    )
    add("")
    if missing:
        add("> Missing stored labels: " + ", ".join(f"`{label}`" for label in missing) + ".")
        add("")

    # -- runs -------------------------------------------------------------
    add("## Runs")
    add("")
    add("| group | label | stored `eval_config` | eval_run id | started (UTC) | audits read | audits from |")
    add("| --- | --- | --- | --- | --- | ---: | --- |")
    for group, labels in GROUPS:
        for run in _selected(runs, labels):
            started = run.started_at.strftime("%Y-%m-%d %H:%M") if run.started_at else "—"
            add(
                f"| {group} | `{run.label}` | `{run.config}` | `{run.id}` | {started} | "
                f"{len(run.audits)} | {run.audit_source} |"
            )
    add("")

    # -- metrics ----------------------------------------------------------
    add("## Stored metrics, val2 beside the earlier runs")
    add("")
    add(
        "Totals are `eval_runs.metrics.totals` exactly as the harness stored them. "
        "`matched` is planted issues the scored findings satisfied; a rejected finding is "
        "not scored, so it can never count here."
    )
    add("")
    add(
        "| group | label | configuration | precision | recall | false-positive rate | "
        "evidence accuracy | severity accuracy | cross-file recall | matched | findings | "
        "cost | runtime (s) |"
    )
    add("| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for group, labels in GROUPS:
        for run in _selected(runs, labels):
            totals = run.totals
            runtime = totals.get("runtime_ms")
            add(
                f"| {group} | `{run.label}` | `{run.config}` | "
                f"{_f(totals.get('precision'))} | {_f(totals.get('recall'))} | "
                f"{_f(totals.get('false_positive_rate'))} | "
                f"{_f(totals.get('evidence_accuracy'))} | "
                f"{_f(totals.get('severity_accuracy'))} | "
                f"{_f(totals.get('cross_file_recall'))} | "
                f"{totals.get('matched', 0)} | {totals.get('findings', 0)} | "
                f"{_money(totals.get('cost_usd'))} | "
                f"{_f(float(runtime) / 1000, 1) if runtime is not None else 'not recorded'} |"
            )
    add("")

    # -- validation outcomes ----------------------------------------------
    add("## Validation outcome per run")
    add("")
    add(
        "The first columns are the validator's own counts, summed from "
        "`eval_runs.metrics.agents.*.validation`. The last three are the stored "
        "`findings.verification_status` of every finding in the run's audits, which is "
        "what the scored rows and the report actually see."
    )
    add("")
    add(
        "| label | configuration | checked | verified | rejected | unresolved | model calls | "
        "validator cost | stored verified | stored rejected | stored unresolved | other statuses |"
    )
    add("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
    for group, labels in GROUPS:
        for run in _selected(runs, labels):
            stored = _stored_validation(run)
            other = {
                status: count
                for status, count in run.statuses.items()
                if status not in STATUSES
            }
            other_text = ", ".join(f"{name}={count}" for name, count in sorted(other.items())) or "—"
            add(
                f"| `{run.label}` | {run.config} | {stored['checked']} | {stored['verified']} | "
                f"{stored['rejected']} | {stored['unresolved']} | {stored['calls']} | "
                f"{_money(stored['cost_usd'])} | {run.statuses.get('verified', 0)} | "
                f"{run.statuses.get('rejected_false_positive', 0)} | "
                f"{run.statuses.get('unresolved', 0)} | {other_text} |"
            )
    add("")

    # -- rejected findings ------------------------------------------------
    add("## Rejected findings")
    add("")
    add(
        "Every finding stored as `rejected_false_positive`, with the semantic check that "
        "produced the verdict. `matches` names the planted issue the finding's category, "
        "file and lines would have satisfied under `benchmark/harness/matching.py`; a "
        "rejected finding that matches was real signal the validator threw away. Raw "
        f"replies are truncated to {RAW_LIMIT} characters here and stored in full in "
        "`validation_checks.raw_reply`."
    )
    add("")
    any_rejected = False
    for group, labels in GROUPS:
        selected = [run for run in _selected(runs, labels) if run.rejected]
        if not selected:
            continue
        any_rejected = True
        add(f"### {group}")
        add("")
        for run in selected:
            add(f"#### `{run.label}` ({run.config}, `{run.id}`)")
            add("")
            add("| repo | category | finding | location | verdict | quote | matches | raw reply |")
            add("| --- | --- | --- | --- | --- | --- | --- | --- |")
            for item in run.rejected:
                check = item.get("check") or {}
                span = item["evidence"][0] if item["evidence"] else None
                location = f"`{span.file_path}:{span.line_start}-{span.line_end}`" if span else "—"
                add(
                    f"| {_cell(item.get('repo'))} | {_cell(item.get('category'))} | "
                    f"`{item['finding_id'][:8]}` {_cell(item.get('title'), 90)} | "
                    f"{location} | {_cell(check.get('verdict'))} | "
                    f"{_cell(check.get('quote'), 120)} | "
                    f"{', '.join(f'`{issue}`' for issue in item['matched_issues']) or '—'} | "
                    f"{_cell(check.get('raw_reply'), RAW_LIMIT)} |"
                )
            add("")
    if not any_rejected:
        add("No finding was stored as rejected in any of these runs.")
        add("")

    # -- join count --------------------------------------------------------
    add("## Rejected findings that matched a planted issue")
    add("")
    add(
        "The join is on the finding id: `validation_checks.finding_id` identifies the "
        "checked finding, and that finding's stored evidence is tested against "
        "`benchmark/expected.json` with the harness's own rule (category, file, "
        "overlapping lines, plus the cycle rule)."
    )
    add("")
    add(
        "| label | configuration | rejected findings stored | with a semantic check | "
        "with a quote | matching a planted issue | issue ids |"
    )
    add("| --- | --- | ---: | ---: | ---: | ---: | --- |")
    for group, labels in GROUPS:
        for run in _selected(runs, labels):
            rejected = run.rejected
            with_check = sum(1 for item in rejected if item.get("check"))
            with_quote = sum(1 for item in rejected if (item.get("check") or {}).get("quote"))
            matched = [item for item in rejected if item["matched_issues"]]
            issue_ids = sorted({issue for item in matched for issue in item["matched_issues"]})
            add(
                f"| `{run.label}` | {run.config} | {len(rejected)} | {with_check} | "
                f"{with_quote} | {len(matched)} | "
                f"{', '.join(f'`{issue}`' for issue in issue_ids) or '—'} |"
            )
    add("")

    # -- failed calls ------------------------------------------------------
    add("## Model calls that failed or were rate limited")
    add("")
    add(
        "`agent_runs` stores one row per gateway attempt with its status, so a failed or "
        "rate-limited call is a row whose status is not `succeeded`. A validator call that "
        "failed leaves its check row with a NULL `raw_reply`, and the reason is in the "
        "`finding_events` note the validator wrote."
    )
    add("")
    for group, labels in GROUPS:
        for run in _selected(runs, labels):
            failed = sum(int(row["n"]) for row in run.agent_failures)
            no_reply = sum(1 for item in run.checks if item.get("raw_reply") is None)
            add(
                f"- `{run.label}` ({run.config}): {failed} failed `agent_runs` row(s), "
                f"{len(run.validator_failures)} validator call(s) failed, "
                f"{no_reply} check row(s) without a stored reply."
            )
            for row in run.agent_failures:
                add(
                    f"  - agent `{row['agent']}` model `{row['model']}` "
                    f"status `{row['status']}` × {row['n']}"
                )
            for row in run.validator_failures[:5]:
                add(f"  - validator: {_cell(row['note'], 200)}")
    add("")

    # -- integrity ---------------------------------------------------------
    add("## Integrity checks")
    add("")
    checks: list[tuple[bool, str]] = []
    if missing:
        checks.append((False, "Missing stored labels: " + ", ".join(missing)))
    for group, labels in GROUPS:
        if group.startswith("repeat"):
            continue
        counts = Counter(run.label for run in runs if run.label in labels)
        duplicated = [label for label, count in counts.items() if count > 1]
        checks.append(
            (
                not duplicated,
                f"{group}: one stored `eval_runs` row per label"
                + (f" (duplicated: {', '.join(duplicated)})" if duplicated else ""),
            )
        )

    selected_runs = [run for _, labels in GROUPS for run in _selected(runs, labels)]
    for run in selected_runs:
        if run.label in AMBIGUOUS_LABELS:
            # The label was reused across configurations, so only the rows that name
            # this eval_run are trusted and a repository with no matched finding and
            # no check cannot be resolved. Reported, not failed.
            checks.append(
                (
                    True,
                    f"`{run.label}` ({run.config}): {len(run.audits)} audit(s) read from the "
                    "row's own links; an ambiguous label cannot be resolved further "
                    f"({run.audit_source})",
                )
            )
            continue
        checks.append(
            (
                len(run.audits) == 4,
                f"`{run.label}` ({run.config}): {len(run.audits)} audit(s) read, expected 4 "
                f"({run.audit_source})",
            )
        )
    for run in selected_runs:
        if run.config != "multi_agent_validated":
            continue
        bad_verdicts = sorted(
            {item.get("verdict") for item in run.checks if item.get("verdict") not in VERDICTS}
        )
        checks.append(
            (
                not bad_verdicts,
                f"`{run.label}`: every stored verdict is one of {', '.join(VERDICTS)}"
                + (f" (found: {', '.join(map(str, bad_verdicts))})" if bad_verdicts else ""),
            )
        )
        wrong = [
            item["finding_id"]
            for item in run.rejected
            if item.get("check") and item["check"].get("verdict") != "contradicted"
        ]
        checks.append(
            (
                not wrong,
                f"`{run.label}`: every rejected finding's check says `contradicted`"
                + (f" ({len(wrong)} do not)" if wrong else ""),
            )
        )
        missing_quote = [
            item["finding_id"]
            for item in run.rejected
            if item.get("check") and not item["check"].get("quote")
        ]
        checks.append(
            (
                not missing_quote,
                f"`{run.label}`: every rejected finding's check stores its quote"
                + (f" ({len(missing_quote)} without)" if missing_quote else ""),
            )
        )
    for run in selected_runs:
        checks.append(
            (
                run.result_rows == 18,
                f"`{run.label}` ({run.config}): {run.result_rows} stored result row(s), "
                "expected 18",
            )
        )
        checks.append(
            (
                run.orphan_matched_ids == 0,
                f"`{run.label}` ({run.config}): every matched finding id belongs to "
                f"this run ({run.orphan_matched_ids} orphan(s))",
            )
        )
    for run in selected_runs:
        checks.append(
            (
                run.rejected_detected == 0,
                f"`{run.label}` ({run.config}): no rejected finding is scored as detected "
                f"({run.rejected_detected})",
            )
        )

    add("| check | result |")
    add("| --- | --- |")
    for passed, description in checks:
        add(f"| {description} | {'PASS' if passed else 'FAIL'} |")
    add("")

    add("## Notes on method")
    add("")
    add(
        "- A rejected finding is excluded from scoring by `_load_findings` in "
        "`benchmark/harness/runner.py`, so it can lower recall and never raises "
        "precision; the join above is what measures whether a rejection removed real "
        "signal."
    )
    add(
        "- `validation_checks` stores one row per semantic attempt that had a readable "
        "cited block: the cited file and line range, the verdict, the quote, the full "
        "masked reply, the latency and the cost. A call that failed keeps its row with a "
        "NULL `raw_reply`."
    )
    add(
        "- A deterministic rejection (missing file, snippet not at the cited lines) is "
        "final and makes no model call, so it has no `validation_checks` row; its reason "
        "is in `finding_events`."
    )
    add(
        "- The `val2-1..3` labels are stored twice: once by an earlier session whose "
        "process was interrupted, and once by the re-run that produced this report. The "
        "Runs table keeps both, by `eval_run id` and start time, rather than pretending "
        "either one was overwritten."
    )
    add(
        "- `val2-*` audits are read back through the check rows the harness linked by "
        "`eval_run_id`; `valid-*` runs predate that table, so their audits come from the "
        "findings their `eval_results`/`eval_extras` rows name, with the configuration's "
        "audits between run starts as the fallback the Runs table reports."
    )
    add(
        "- `repeat-*` labels were reused across different configurations on 2026-11-10, "
        "so those rows are listed as stored, each with the configuration it actually ran; "
        "`stage3-multi_agent-1..3` is the clean three-run `multi_agent` comparison."
    )
    add("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="markdown file to write")
    args = parser.parse_args(argv)
    try:
        runs, missing, _manifest = load()
    except ManifestError as exc:
        print(f"manifest error: {exc}")
        return 1
    output = render(runs, missing)
    Path(args.out).write_text(output + "\n", encoding="utf-8")
    print(f"wrote {args.out} ({len(output.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
