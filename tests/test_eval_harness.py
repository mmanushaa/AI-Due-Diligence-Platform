"""Step 2.6: the harness's arithmetic, checked without a database or Docker.

The runner is exercised end to end by running the harness itself; these tests pin
down the parts whose numbers a reader is asked to verify by hand — the matching
rule, the duplicate-versus-false-positive split, the rates and the manifest.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from benchmark.harness import configs, evidence, matching, metrics
from benchmark.harness.expected import ExpectedIssue, ManifestError, load_manifest

BENCHMARK = Path(__file__).resolve().parents[1] / "benchmark"
REPOS = BENCHMARK / "repos"


def issue(**overrides) -> ExpectedIssue:
    base = dict(
        id="x-1",
        repo="python_app",
        category="security",
        title="example",
        file="app/database.py",
        line_start=30,
        line_end=40,
        severity="high",
        detector="static analysis",
    )
    base.update(overrides)
    return ExpectedIssue(**base)


def finding(
    category: str,
    path: str,
    start: int,
    end: int,
    severity: str = "high",
    title: str | None = None,
    reasoning: str = "",
) -> matching.ReportedFinding:
    return matching.ReportedFinding(
        category=category,
        severity=severity,
        evidence=(matching.Span(file_path=path, line_start=start, line_end=end),),
        title=f"{category} at {path}" if title is None else title,
        reasoning=reasoning,
    )


# --------------------------------------------------------------------------- #
# the matching rule
# --------------------------------------------------------------------------- #


def test_a_finding_needs_the_same_category_file_and_an_overlapping_line():
    target = issue()
    assert matching.matches(target, finding("security", "app/database.py", 38, 38))
    # one shared line is enough, in either direction
    assert matching.matches(target, finding("security", "app/database.py", 40, 99))
    # touching but not overlapping is a miss
    assert not matching.matches(target, finding("security", "app/database.py", 41, 41))
    # wrong category, right place
    assert not matching.matches(target, finding("dependency", "app/database.py", 38, 38))
    # right category, wrong file
    assert not matching.matches(target, finding("security", "app/other.py", 38, 38))


def test_the_reported_path_may_carry_a_snapshot_root():
    assert matching.matches(issue(), finding("security", "/repo/root/app/database.py", 38, 38))
    assert matching.matches(
        issue(file="src\\config.js"), finding("security", "src/config.js", 30, 30)
    )


def test_one_finding_cannot_satisfy_two_issues_and_one_issue_takes_one_finding():
    issues = (issue(id="a", line_start=30, line_end=31), issue(id="b", line_start=31, line_end=32))
    findings = (finding("security", "app/database.py", 31, 31),)

    outcome = matching.match_all(issues, findings)

    assert [assignment.issue.id for assignment in outcome.assignments] == ["a"]
    assert [missed.id for missed in outcome.missed] == ["b"]
    assert outcome.false_positives == ()


def test_a_second_report_of_a_matched_issue_is_a_duplicate_not_a_false_positive():
    issues = (issue(),)
    findings = (
        finding("security", "app/database.py", 38, 38),
        finding("security", "app/database.py", 38, 38),
        finding("security", "app/unrelated.py", 5, 5),
    )

    outcome = matching.match_all(issues, findings)

    assert len(outcome.assignments) == 1
    assert len(outcome.duplicates) == 1
    assert len(outcome.false_positives) == 1
    assert outcome.distinct_reported == 2


def test_findings_are_ordered_strongest_first_so_the_run_is_reproducible():
    findings = (
        finding("dependency", "requirements.txt", 3, 3, severity="medium"),
        finding("dependency", "requirements.txt", 3, 3, severity="high"),
        finding("dependency", "requirements.txt", 3, 3, severity="low"),
    )

    ordered = matching.order_findings(findings)

    assert [item.severity for item in ordered] == ["high", "medium", "low"]


# --------------------------------------------------------------------------- #
# cycle matching
# --------------------------------------------------------------------------- #


def manifest() -> Manifest:
    return load_manifest(BENCHMARK / "expected.json", REPOS)


def cycle_issue(issue_id: str) -> ExpectedIssue:
    return next(item for item in manifest().issues if item.id == issue_id)


def test_a_finding_in_either_file_of_the_python_cycle_matches():
    target = cycle_issue("py-cycle-01")
    # orders.py at the import the manifest names
    assert matching.matches(
        target, finding("architecture", "app/orders.py", 5, 5, title="Circular import")
    )
    # billing.py at a line the manifest never names: a cycle spans files
    assert matching.matches(
        target, finding("architecture", "app/billing.py", 12, 12, title="Circular import")
    )
    # the wording may live in reasoning instead of the title
    assert matching.matches(
        target,
        finding(
            "architecture",
            "app/billing.py",
            1,
            1,
            title="Architecture concern",
            reasoning="Orders and billing import each other, a circular dependency.",
        ),
    )


def test_a_finding_in_either_file_of_the_javascript_cycle_matches():
    target = cycle_issue("js-cycle-01")
    assert matching.matches(
        target, finding("architecture", "src/lib/queue.js", 3, 3, title="Circular require")
    )
    assert matching.matches(
        target, finding("architecture", "src/lib/worker.js", 9, 9, title="Cyclic dependency")
    )


def test_an_architecture_finding_outside_the_cycle_does_not_match():
    target = cycle_issue("py-cycle-01")
    assert not matching.matches(
        target, finding("architecture", "app/config.py", 5, 5, title="Circular import")
    )
    js_target = cycle_issue("js-cycle-01")
    assert not matching.matches(
        js_target, finding("architecture", "src/server.js", 3, 3, title="Circular require")
    )


def test_a_cycle_file_without_cycle_wording_does_not_match():
    """An oversized-module finding in orders.py is not the cycle."""
    target = cycle_issue("py-cycle-01")
    assert not matching.matches(
        target,
        finding(
            "architecture",
            "app/orders.py",
            5,
            5,
            title="Module has too many responsibilities",
            reasoning="This file mixes persistence and presentation.",
        ),
    )
    assert not matching.matches(
        target, finding("architecture", "app/billing.py", 3, 3, title="Long function")
    )


def test_a_finding_in_another_repository_does_not_match():
    python_target = cycle_issue("py-cycle-01")
    assert not matching.matches(
        python_target, finding("architecture", "src/lib/queue.js", 3, 3, title="Circular require")
    )
    js_target = cycle_issue("js-cycle-01")
    assert not matching.matches(
        js_target, finding("architecture", "app/orders.py", 5, 5, title="Circular import")
    )


def test_a_security_finding_in_a_cycle_file_does_not_match():
    target = cycle_issue("py-cycle-01")
    assert not matching.matches(
        target, finding("security", "app/orders.py", 5, 5, title="Circular import")
    )


def test_a_matching_issue_without_cycle_files_keeps_the_line_rule():
    plain = issue(
        id="plain-arch-01", category="architecture", file="app/orders.py", line_start=5, line_end=5
    )
    assert plain.cycle_files == ()
    assert matching.matches(plain, finding("architecture", "app/orders.py", 5, 5))
    # same category and cycle wording, but no cycle_files: still a different file
    assert not matching.matches(
        plain, finding("architecture", "app/billing.py", 5, 5, title="Circular import")
    )


def test_a_cycle_file_missing_from_the_repo_is_rejected(tmp_path: Path):
    raw = json.loads((BENCHMARK / "expected.json").read_text(encoding="utf-8"))
    target = next(item for item in raw["issues"] if item["id"] == "py-cycle-01")
    target["cycle_files"] = ["app/orders.py", "app/ghost.py"]
    broken = tmp_path / "expected.json"
    broken.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ManifestError, match="cycle file app/ghost.py"):
        load_manifest(broken, REPOS)


# --------------------------------------------------------------------------- #
# the rates
# --------------------------------------------------------------------------- #


def score(**overrides) -> metrics.RepoScore:
    base = dict(
        repo="r",
        expected=3,
        findings=4,
        matched=2,
        duplicates=1,
        evidence_correct=2,
        severity_correct=1,
        cross_file_expected=2,
        cross_file_matched=1,
        cost_usd=Decimal("0.0120"),
        runtime_ms=1500,
    )
    base.update(overrides)
    return metrics.RepoScore(**base)


def test_rates_come_from_the_counts_stored_next_to_them():
    result = score()
    assert result.recall == 0.6667  # 2 / 3
    assert result.distinct_reported == 3  # 2 matched + 1 false positive
    assert result.precision == 0.6667  # 2 / 3
    assert result.false_positive_rate == 0.3333  # 1 / 3
    assert result.evidence_accuracy == 1.0  # 2 / 2
    assert result.severity_accuracy == 0.5  # 1 / 2
    assert result.cross_file_recall == 0.5  # 1 / 2
    assert result.fractions()["precision"] == "2/3"


def test_a_zero_denominator_is_reported_as_unknown_not_as_zero_or_one():
    clean = score(
        expected=0, matched=0, findings=0, duplicates=0, evidence_correct=0, severity_correct=0
    )

    assert clean.recall is None
    assert clean.precision is None
    assert clean.false_positive_rate is None
    assert clean.evidence_accuracy is None
    assert clean.fractions()["recall"] == "n/a"


def test_totals_sum_counts_and_recompute_rates_instead_of_averaging_them():
    first = score(
        expected=1, findings=1, matched=1, duplicates=0, evidence_correct=1, severity_correct=1
    )
    second = score(
        expected=9,
        findings=9,
        matched=0,
        duplicates=0,
        evidence_correct=0,
        severity_correct=0,
        cross_file_expected=0,
        cross_file_matched=0,
    )

    total = metrics.totals((first, second))

    assert total.expected == 10 and total.matched == 1 and total.findings == 10
    assert total.recall == 0.1  # 1 / 10, not the mean of 1.0 and 0.0
    assert total.precision == 0.1
    assert total.evidence_accuracy == 1.0


# --------------------------------------------------------------------------- #
# evidence accuracy
# --------------------------------------------------------------------------- #


def test_evidence_must_cite_the_line_the_claim_is_about(tmp_path: Path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "database.py").write_text(
        "line 1\nline 2\n"
        "query = \"SELECT id FROM orders WHERE status = '%s'\" % status\n"
        "cursor.execute(query)\n",
        encoding="utf-8",
    )
    target = issue(line_start=3, line_end=3, sink_pattern="%\\s*status\\b")

    assert evidence.evidence_is_correct(
        target, finding("security", "app/database.py", 3, 3), tmp_path
    )
    # right file and overlapping range, but the quoted line is the execute(), not the query
    assert not evidence.evidence_is_correct(
        target, finding("security", "app/database.py", 4, 4), tmp_path
    )


def test_a_secret_is_verified_by_token_shape_because_the_snippet_is_masked(tmp_path: Path):
    (tmp_path / "app").mkdir()
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    (tmp_path / "app" / "config.py").write_text(f"TOKEN = {token!r}\n", encoding="utf-8")
    target = issue(
        category="secret",
        file="app/config.py",
        line_start=1,
        line_end=1,
        notes=f"Fake token {token}: ghp_ plus 36 characters.",
    )

    assert evidence.evidence_is_correct(target, finding("secret", "app/config.py", 1, 1), tmp_path)


def test_a_dependency_is_verified_by_package_and_version(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("flask==3.0.3\njinja2==2.11.2\n", encoding="utf-8")
    target = issue(
        category="dependency",
        file="requirements.txt",
        line_start=2,
        line_end=2,
        package="jinja2",
        version="2.11.2",
    )

    assert evidence.evidence_is_correct(
        target, finding("dependency", "requirements.txt", 2, 2), tmp_path
    )
    assert not evidence.evidence_is_correct(
        target, finding("dependency", "requirements.txt", 1, 1), tmp_path
    )


def test_evidence_never_reads_outside_the_snapshot(tmp_path: Path):
    span = matching.Span(file_path="../../etc/passwd", line_start=1, line_end=2)
    assert evidence.cited_lines(tmp_path, span) == []


# --------------------------------------------------------------------------- #
# the manifest and the configurations
# --------------------------------------------------------------------------- #


def test_the_manifest_agrees_with_the_fixtures():
    manifest = load_manifest(BENCHMARK / "expected.json", REPOS)

    assert manifest.version == "0.5.0"
    assert manifest.repos == ("adversarial_app", "clean_app", "js_app", "python_app")
    # clean_app is the false-positive control: nothing planted, so every finding there is false
    assert manifest.for_repo("clean_app") == ()
    # at least two cross-file issues in the adversarial fixture, on top of the two older ones
    adversarial = [item.id for item in manifest.for_repo("adversarial_app") if item.cross_file]
    assert len(adversarial) >= 2
    assert {item.id for item in manifest.cross_file_issues} >= {
        "py-authz-01",
        "js-authz-01",
        "adv-export-authz-01",
        "adv-delete-authz-02",
        # a cycle spans files, so it counts as cross-file
        "py-cycle-01",
        "js-cycle-01",
    }
    # the answer key names the members of each cycle
    assert cycle_issue("py-cycle-01").cycle_files == ("app/orders.py", "app/billing.py")
    assert cycle_issue("js-cycle-01").cycle_files == ("src/lib/queue.js", "src/lib/worker.js")


def test_a_manifest_that_cites_a_line_outside_the_file_is_rejected(tmp_path: Path):
    manifest = tmp_path / "expected.json"
    raw = json.loads((BENCHMARK / "expected.json").read_text(encoding="utf-8"))
    raw["issues"][0]["line_end"] = 9999
    manifest.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ManifestError, match="cites line 9999"):
        load_manifest(manifest, REPOS)


def test_scanners_only_runs_today_and_agent_configurations_explain_themselves():
    scanners_only = configs.get("scanners_only")
    assert configs.availability(scanners_only) == (True, None)
    assert scanners_only in configs.runnable()
    # step 2.7 registered the baseline, so this configuration is live now
    assert configs.availability(configs.get("single_agent")) == (True, None)

    for configuration in configs.CONFIGURATIONS:
        available, reason = configs.availability(configuration)
        assert available or reason, f"{configuration.name} must either run or say why not"
        if not available:
            assert configs.AGENT_REGISTRY in (reason or "")


def test_an_unknown_configuration_is_refused_by_name():
    with pytest.raises(configs.ConfigurationUnavailable, match="unknown configuration"):
        configs.get("scanners_and_vibes")
