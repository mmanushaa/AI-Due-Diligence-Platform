"""Step 2.6b-D: a real run must keep its matched finding ids resolvable.

This goes through ``run_configuration`` itself rather than the persistence helper,
because the bug it guards was an ordering bug: the audits were pruned before the
run was written, so every ``matched_finding_id`` dangled and the insert failed on
its own foreign key.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text

from app.db.session import get_session_factory
from app.scanners.gitleaks import GITLEAKS_IMAGE
from app.scanners.osv import OSV_IMAGE
from app.scanners.scancode import SCANCODE_IMAGE
from app.scanners.semgrep import SEMGREP_IMAGE
from benchmark.harness import configs, runner
from benchmark.harness.expected import load_manifest

BENCHMARK = Path(__file__).resolve().parents[1] / "benchmark"
REPOS = BENCHMARK / "repos"
REPO = "adversarial_app"
DOCKER = shutil.which("docker")
IMAGES = (GITLEAKS_IMAGE, SEMGREP_IMAGE, OSV_IMAGE, SCANCODE_IMAGE)


def _image_present(image: str) -> bool:
    if DOCKER is None:
        return False
    return (
        subprocess.run(
            ["docker", "image", "inspect", image], capture_output=True, check=False
        ).returncode
        == 0
    )


requires_scanners = pytest.mark.skipif(
    not all(_image_present(image) for image in IMAGES),
    reason="every scanner image is required for a full audit run",
)


@requires_scanners
def test_a_persisted_run_keeps_matched_ids_resolvable():
    manifest = load_manifest(BENCHMARK / "expected.json", REPOS)
    factory = get_session_factory()

    record = runner.run_configuration(
        configs.get("scanners_only"),
        manifest,
        repos=(REPO,),
        repos_root=REPOS,
        persist=True,
    )
    run_id = record.eval_run_id
    assert run_id is not None, "a persisted run must have an eval_runs id"

    try:
        with factory() as session:
            rows = (
                session.execute(
                    text(
                        "SELECT expected_issue_id, detected, matched_finding_id,"
                        " matched_file, matched_line_start"
                        " FROM eval_results WHERE eval_run_id = :run"
                    ),
                    {"run": run_id},
                )
                .mappings()
                .all()
            )
            assert rows, "the run wrote no eval_results"

            matched = [row for row in rows if row["detected"]]
            assert matched, "no expected issue was detected, so the id check proves nothing"

            for row in matched:
                assert row["matched_finding_id"] is not None, (
                    f"{row['expected_issue_id']} was detected but names no finding"
                )
                resolved = (
                    session.execute(
                        text(
                            "SELECT e.file_path, e.line_start FROM findings f"
                            " JOIN evidence e ON e.finding_id = f.id WHERE f.id = :finding"
                        ),
                        {"finding": row["matched_finding_id"]},
                    )
                    .mappings()
                    .all()
                )
                assert resolved, (
                    f"{row['expected_issue_id']} points at {row['matched_finding_id']},"
                    " which is not a finding any more"
                )
                assert any(
                    item["file_path"].endswith(row["matched_file"])
                    and item["line_start"] == row["matched_line_start"]
                    for item in resolved
                ), f"{row['expected_issue_id']} names a different location than the finding holds"

            orphans = session.execute(
                text(
                    "SELECT count(*) FROM eval_results"
                    " WHERE matched_finding_id IS NOT NULL"
                    " AND matched_finding_id NOT IN (SELECT id FROM findings)"
                )
            ).scalar_one()
            assert orphans == 0
    finally:
        prefix = f"eval-{configs.get('scanners_only').name}-{REPO}-%"
        with factory() as cleanup:
            cleanup.execute(text("DELETE FROM eval_runs WHERE id = :run"), {"run": run_id})
            cleanup.execute(
                text(
                    "DELETE FROM audits WHERE repository_id IN"
                    " (SELECT id FROM repositories WHERE name LIKE :prefix)"
                ),
                {"prefix": prefix},
            )
            cleanup.execute(
                text("DELETE FROM repositories WHERE name LIKE :prefix"), {"prefix": prefix}
            )
            cleanup.commit()
