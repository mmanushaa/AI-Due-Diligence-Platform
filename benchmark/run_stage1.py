#!/usr/bin/env python3
"""Stage 1 gate: audit every fixture repo through the API and compare the
stored findings against benchmark/expected.json.

Usage:
    python benchmark/run_stage1.py [--api http://127.0.0.1:8000]

Requires a running API and a running Celery worker (the audits are executed by
the worker). The fixture repositories are only ever read and zipped: they are
never executed, imported or handed to a shell.

Exit status is 1 when an expected issue was not found, 0 otherwise. Extra
findings are reported for manual review but do not fail the gate.

Matching is by category, file path and overlapping line range:
an expected issue matches the first finding with the same category, the same
file (path suffix, so "app/config.py" matches a snapshot root) and a line range
that shares at least one line. Each finding can satisfy at most one expected
issue, and each expected issue can consume at most one finding.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPOS = HERE / "repos"
EXPECTED_FILE = HERE / "expected.json"

POLL_SECONDS = 2.0
AUDIT_TIMEOUT_SECONDS = 900.0

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


# --------------------------------------------------------------------------- #
# minimal API client (stdlib only, no extra dependencies)
# --------------------------------------------------------------------------- #


class ApiError(RuntimeError):
    pass


def _request(method: str, url: str, data: bytes | None = None, headers: dict | None = None) -> bytes:
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        raise ApiError(f"{method} {url} -> HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"{method} {url} -> {exc.reason}") from exc


def _json(method: str, url: str, data: bytes | None = None, headers: dict | None = None):
    return json.loads(_request(method, url, data, headers) or b"null")


def check_health(api: str) -> None:
    payload = _json("GET", f"{api}/health")
    checks = payload.get("checks", {})
    broken = [name for name, value in checks.items() if value.get("status") != "ok"]
    if broken:
        raise ApiError(f"API is up but unhealthy: {', '.join(broken)}")


def zip_repository(repo_dir: Path) -> bytes:
    """Zip a fixture in memory. Nothing is written outside the process."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(repo_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(repo_dir).as_posix())
    return buffer.getvalue()


def upload_repository(api: str, name: str, archive: bytes) -> dict:
    boundary = f"----stage1{uuid.uuid4().hex}"
    parts = []
    for field, value in (("name", name),):
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{name}.zip"\r\n'
        "Content-Type: application/zip\r\n\r\n".encode()
        + archive
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    return _json(
        "POST",
        f"{api}/api/v1/repositories",
        body,
        {"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )


def start_audit(api: str, repository_id: str) -> dict:
    return _json("POST", f"{api}/api/v1/repositories/{repository_id}/audits")


def wait_for_audit(api: str, audit_id: str) -> dict:
    deadline = time.monotonic() + AUDIT_TIMEOUT_SECONDS
    last = {}
    while time.monotonic() < deadline:
        last = _json("GET", f"{api}/api/v1/audits/{audit_id}")
        if last.get("status") in TERMINAL_STATUSES:
            return last
        time.sleep(POLL_SECONDS)
    raise ApiError(
        f"audit {audit_id} did not finish within {AUDIT_TIMEOUT_SECONDS:.0f}s "
        f"(last status: {last.get('status')}, stage: {last.get('current_stage')})"
    )


def fetch_findings(api: str, audit_id: str) -> list[dict]:
    payload = _json("GET", f"{api}/api/v1/audits/{audit_id}/findings")
    return payload.get("findings", [])


# --------------------------------------------------------------------------- #
# comparison
# --------------------------------------------------------------------------- #


def normalise(path: str | None) -> str:
    if not path:
        return ""
    cleaned = path.replace("\\", "/").lstrip("./")
    return cleaned


def same_file(expected: str, actual: str | None) -> bool:
    """Compare paths from the snapshot root, tolerating a leading directory."""
    left, right = normalise(expected), normalise(actual)
    if not left or not right:
        return False
    return left == right or right.endswith("/" + left) or left.endswith("/" + right)


def lines_overlap(a_start, a_end, b_start, b_end) -> bool:
    if None in (a_start, a_end, b_start, b_end):
        return False
    return not (a_end < b_start or a_start > b_end)


@dataclass
class Match:
    issue: dict
    finding: dict


def match_issues(expected: list[dict], findings: list[dict]) -> tuple[list[Match], list[dict], list[dict]]:
    """Greedy, one-to-one match by category, file and overlapping lines."""
    used: set[int] = set()
    matches: list[Match] = []
    missed: list[dict] = []

    for issue in expected:
        best_index = None
        best_overlap = -1
        for index, finding in enumerate(findings):
            if index in used:
                continue
            if finding.get("category") != issue.get("category"):
                continue
            if not same_file(issue["file"], finding.get("file_path")):
                continue
            if not lines_overlap(
                issue["line_start"],
                issue["line_end"],
                finding.get("line_start"),
                finding.get("line_end"),
            ):
                continue
            overlap = min(issue["line_end"], finding["line_end"]) - max(
                issue["line_start"], finding["line_start"]
            ) + 1
            if overlap > best_overlap:
                best_overlap = overlap
                best_index = index

        if best_index is None:
            missed.append(issue)
        else:
            used.add(best_index)
            matches.append(Match(issue=issue, finding=findings[best_index]))

    extras = [f for i, f in enumerate(findings) if i not in used]
    return matches, missed, extras


def explain_miss(issue: dict, findings: list[dict]) -> str:
    """Say why an expected issue did not match, in terms a reviewer can act on."""
    same_category = [f for f in findings if f.get("category") == issue.get("category")]
    if not same_category:
        return f"no {issue['category']} finding at all"
    same_file_findings = [f for f in same_category if same_file(issue["file"], f.get("file_path"))]
    if not same_file_findings:
        files = sorted({normalise(f.get("file_path")) for f in same_category})
        return "category found only in: " + ", ".join(files[:4])
    ranges = ", ".join(f"{f.get('line_start')}-{f.get('line_end')}" for f in same_file_findings[:4])
    return f"right file, lines {issue['line_start']}-{issue['line_end']} vs {ranges}"


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #


def format_location(item: dict) -> str:
    start, end = item.get("line_start"), item.get("line_end")
    where = f"{normalise(item.get('file_path'))}:{start}" if start is not None else normalise(
        item.get("file_path")
    )
    if end is not None and end != start:
        where += f"-{end}"
    return where


def run(api: str) -> int:
    expected_doc = json.loads(EXPECTED_FILE.read_text(encoding="utf-8"))
    expected = expected_doc["issues"]

    repos = sorted({issue["repo"] for issue in expected})
    missing_dirs = [name for name in repos if not (REPOS / name).is_dir()]
    if missing_dirs:
        print(f"missing fixture repositories: {', '.join(missing_dirs)}", file=sys.stderr)
        return 2

    check_health(api)

    print(f"Stage 1 gate — {len(expected)} expected issues across {len(repos)} repositories")
    print(f"API: {api}\n")

    all_matched: list[Match] = []
    all_missed: list[tuple[str, dict, str]] = []
    all_extras: list[tuple[str, dict]] = []

    for repo in repos:
        repo_expected = [issue for issue in expected if issue["repo"] == repo]
        archive = zip_repository(REPOS / repo)
        print(f"== {repo} ==")
        print(f"   archive {len(archive):,} bytes, {len(repo_expected)} expected issues")

        uploaded = upload_repository(api, repo, archive)
        audit = start_audit(api, uploaded["id"])
        finished = wait_for_audit(api, str(audit["id"]))
        print(f"   audit {audit['id']} -> {finished.get('status')}")
        if finished.get("status") != "completed":
            print(f"   !! audit did not complete: {finished.get('error') or finished.get('status')}")

        findings = fetch_findings(api, str(audit["id"]))
        matched, missed, extras = match_issues(repo_expected, findings)
        all_matched.extend(matched)
        all_missed.extend((repo, issue, explain_miss(issue, findings)) for issue in missed)
        all_extras.extend((repo, finding) for finding in extras)

        print(f"   findings stored: {len(findings)} | matched {len(matched)} | missed {len(missed)} | extra {len(extras)}\n")

    print("=" * 78)
    print("MATCHED ISSUES")
    print("=" * 78)
    for match in all_matched:
        issue, finding = match.issue, match.finding
        print(
            f"  [ok] {issue['id']:<14} {issue['category']:<12} "
            f"{issue['file']}:{issue['line_start']}-{issue['line_end']}"
        )
        print(f"       detector {issue['detector']!r} -> {finding.get('source_tool') or 'n/a'}")
    if not all_matched:
        print("  (none)")
    print(f"\n  {len(all_matched)}/{len(expected)} expected issues matched")

    print()
    print("=" * 78)
    print("MISSED ISSUES")
    print("=" * 78)
    for repo, issue, reason in all_missed:
        print(
            f"  [MISS] {issue['id']:<14} {repo:<10} {issue['category']:<12} "
            f"{issue['file']}:{issue['line_start']}-{issue['line_end']} "
            f"({issue['severity']}, {issue['detector']})"
        )
        print(f"       {reason}")
    if not all_missed:
        print("  (none)")
    print(f"\n  {len(all_missed)} missed")

    print()
    print("=" * 78)
    print("EXTRA FINDINGS (not in expected.json — review by hand)")
    print("=" * 78)
    for repo, finding in all_extras:
        print(
            f"  [+ ] {repo:<10} {(finding.get('category') or '?'):<12} "
            f"{(finding.get('severity') or '?'):<8} {format_location(finding):<28} "
            f"{(finding.get('source_tool') or '')[:32]}"
        )
        print(f"       {finding.get('title', '')[:110]}")
    if not all_extras:
        print("  (none)")
    print(f"\n  {len(all_extras)} extra findings")

    print()
    print(
        f"summary: expected {len(expected)} | matched {len(all_matched)} | "
        f"missed {len(all_missed)} | extra {len(all_extras)}"
    )
    return 1 if all_missed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 1 benchmark gate")
    parser.add_argument(
        "--api",
        default=os.environ.get("API_BASE_URL", "http://127.0.0.1:8000"),
        help="base URL of the running API (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    try:
        return run(args.api.rstrip("/"))
    except ApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
