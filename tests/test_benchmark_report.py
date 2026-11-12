"""Step 2.6g: the repeated-runs report, tested without a database.

The report's job is arithmetic over stored rows, so the tests feed it rows. That
keeps them fast and, more usefully, lets a number be checked by hand: every
expectation below is a division the reader can redo.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from benchmark import report

START = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def make_run(
    config: str,
    *,
    label: str = "",
    offset: int = 0,
    detected: tuple[str, ...] = (),
    missed: tuple[str, ...] = (),
    recall: float | None = 0.5,
    precision: float | None = 0.5,
    false_positives: int = 1,
    cross_file: float | None = 0.5,
    evidence: float | None = 1.0,
    severity: float | None = 1.0,
    cost: str = "0.0010",
    secs: float = 100.0,
) -> report.RunMetrics:
    totals = {
        "recall": recall,
        "precision": precision,
        "false_positives": false_positives,
        "cross_file_recall": cross_file,
        "evidence_accuracy": evidence,
        "severity_accuracy": severity,
        "cost_usd": cost,
        "matched_ids": list(detected),
        "missed_ids": list(missed),
    }
    return report.RunMetrics(
        run_id=f"{config}-{label}",
        config=config,
        label=label,
        started_at=START + timedelta(minutes=offset),
        totals=totals,
        runtime_ms=int(secs * 1000),
        detected=frozenset(detected),
    )


def test_only_the_last_three_runs_of_a_configuration_are_reported():
    runs = [make_run("multi_agent", label=f"repeat-{n}", offset=n) for n in range(1, 5)]
    summaries = report.latest_per_config(runs, repeats=3)

    assert [run.label for run in summaries["multi_agent"].runs] == [
        "repeat-2",
        "repeat-3",
        "repeat-4",
    ]


def test_a_label_stored_twice_counts_once_with_its_earliest_run():
    runs = [
        make_run("multi_agent", label="repeat-1", offset=0),
        make_run("multi_agent", label="repeat-2", offset=1),
        make_run("multi_agent", label="repeat-3", offset=2),
        make_run("multi_agent", label="repeat-3", offset=3),
    ]
    summary = report.latest_per_config(runs)["multi_agent"]

    assert [run.started_at for run in summary.runs] == [START, START + timedelta(minutes=1), START + timedelta(minutes=2)]
    assert len(summary.runs) == 3


def test_unlabelled_runs_still_fall_back_to_the_latest_three():
    runs = [make_run("clean_app", label="", offset=n) for n in range(5)]
    summary = report.latest_per_config(runs)["clean_app"]

    assert [run.started_at for run in summary.runs] == [
        START + timedelta(minutes=n) for n in (2, 3, 4)
    ]


def test_the_spread_is_the_mean_and_range_of_the_stored_rates():
    runs = [
        make_run("multi_agent", offset=0, recall=0.5, precision=0.6, secs=100, cost="0.0010"),
        make_run("multi_agent", offset=1, recall=0.75, precision=0.8, secs=200, cost="0.0020"),
        make_run("multi_agent", offset=2, recall=1.0, precision=1.0, secs=300, cost="0.0030"),
    ]
    summary = report.latest_per_config(runs)["multi_agent"]

    assert summary.rate_values("recall") == [0.5, 0.75, 1.0]
    assert summary.costs() == [Decimal("0.0010"), Decimal("0.0020"), Decimal("0.0030")]
    assert summary.secs() == [100.0, 200.0, 300.0]

    text = report.render({"multi_agent": summary}, ())
    assert "0.750 (0.500-1.000)" in text  # recall
    assert "0.800 (0.600-1.000)" in text  # precision
    assert "$0.0020 ($0.0010-$0.0030)" in text
    assert "200.00 (100-300)" in text


def test_an_issue_is_counted_only_in_the_runs_that_matched_it():
    runs = [
        make_run("multi_agent", offset=0, detected=("py-authz-01",)),
        make_run("multi_agent", offset=1, missed=("js-authz-01",)),
        make_run("multi_agent", offset=2, detected=("py-authz-01",)),
    ]
    summary = report.latest_per_config(runs)["multi_agent"]

    assert summary.matched("py-authz-01") == 2
    assert summary.matched("js-authz-01") == 0
    text = report.render({"multi_agent": summary}, ("py-authz-01", "js-authz-01"))
    assert "2/3" in text and "0/3" in text


def test_authorization_recall_is_matched_cells_over_four_issues_times_three_runs():
    per_run = ("py-authz-01", "js-authz-01", "adv-export-authz-01")
    runs = [make_run("multi_agent", offset=n, detected=per_run) for n in range(3)]
    summary = report.latest_per_config(runs)["multi_agent"]

    # 3 issues x 3 runs = 9 of 4 x 3 = 12 cells.
    assert summary.authorization_recall() == 0.75
    assert "9/12 = 0.750" in report.render({"multi_agent": summary}, ())


def test_a_rate_the_run_could_not_measure_is_skipped_rather_than_counted_as_zero():
    runs = [
        make_run("clean_app", offset=0, recall=None, precision=None),
        make_run("clean_app", offset=1, recall=None, precision=None),
    ]
    summary = report.latest_per_config(runs)["clean_app"]

    assert summary.rate_values("recall") == []
    assert "not recorded" in report.render({"clean_app": summary}, ())


def test_an_empty_report_says_so_instead_of_printing_a_table_of_zeros():
    text = report.render({}, ())

    assert "no stored runs: not recorded" in text
    assert "Repeated runs" in text


def test_the_agent_mean_covers_only_the_configurations_that_call_a_model():
    summaries = {
        "scanners_only": report.latest_per_config(
            [make_run("scanners_only", detected=())]
        )["scanners_only"],
        "multi_agent": report.latest_per_config(
            [make_run("multi_agent", detected=report.AUTHORIZATION_ISSUES)]
        )["multi_agent"],
        "single_agent_tools": report.latest_per_config(
            [make_run("single_agent_tools", detected=("py-authz-01",))]
        )["single_agent_tools"],
    }

    text = report.render(summaries, ())
    # The scanner floor is reported but does not drag the agents' mean down:
    # multi_agent 1.000 and single_agent_tools 0.250 average to 0.625.
    assert "agent configurations mean" in text
    assert "0.625" in text


def test_the_four_authorization_issues_are_the_planted_access_control_gaps():
    assert report.AUTHORIZATION_ISSUES == (
        "adv-delete-authz-02",
        "adv-export-authz-01",
        "js-authz-01",
        "py-authz-01",
    )
    assert "scanners_only" not in report.AGENT_CONFIGS
    assert "scanners_plus_deterministic" not in report.AGENT_CONFIGS
