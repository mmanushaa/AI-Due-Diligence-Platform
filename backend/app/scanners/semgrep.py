"""Semgrep adapter (step 1.5).

The image bundles the Python, JavaScript and TypeScript registry rulesets plus
the local rules in docker/semgrep/rules, so the scan needs no network. The
adapter runs it with subprocess (argument list, no shell, timeout, read-only
snapshot mount) and normalizes results into the finding schema.
"""

from __future__ import annotations

import json
import logging
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from app.db.enums import (
    Category,
    EvidenceKind,
    FindingOrigin,
    ScannerRunStatus,
    ScannerTool,
    Severity,
    Verdict,
    VerificationStatus,
)
from app.db.models import Audit, Evidence, Finding, ScannerRun
from app.scanners.common import (
    MAX_ERROR_CHARS,
    MAX_SNIPPET_CHARS,
    existing_fingerprints,
    finding_fingerprint,
    redact_text,
)

logger = logging.getLogger(__name__)

SEMGREP_IMAGE = "due-diligence-semgrep:1.182.1"
SNAPSHOT_MOUNT = "/repo"
OUTPUT_MOUNT = "/out"

RULESET_CONFIGS = (
    "/rules/python.yaml",
    "/rules/javascript.yaml",
    "/rules/typescript.yaml",
    "/rules/local/sql.yaml",
)

# Semgrep reports CRITICAL / ERROR / WARNING / INFO. It has no separate
# high-vs-critical or low-vs-info split, so ERROR maps to high (never critical,
# to avoid overstating a rule) and INFO maps straight across. `low` is therefore
# produced by other detectors, not by Semgrep.
SEVERITY_MAP = {
    "CRITICAL": Severity.CRITICAL,
    "ERROR": Severity.HIGH,
    "WARNING": Severity.MEDIUM,
    "INFO": Severity.INFO,
}
DEFAULT_SEVERITY = Severity.MEDIUM


def map_severity(raw: str) -> Severity:
    """Semgrep severity string to our five levels; unknown values become medium."""
    return SEVERITY_MAP.get(raw.strip().upper(), DEFAULT_SEVERITY)


@dataclass(frozen=True)
class SemgrepConfig:
    image: str = SEMGREP_IMAGE
    timeout_seconds: int = 600
    memory_limit: str = "1g"
    cpu_limit: str = "1"


@dataclass(frozen=True)
class SemgrepResult:
    rule_id: str
    severity: str
    message: str
    file: str
    line_start: int
    line_end: int


@dataclass(frozen=True)
class SemgrepRunResult:
    results: tuple[SemgrepResult, ...]
    version: str | None
    exit_code: int
    error: str | None
    report_path: Path | None


@dataclass(frozen=True)
class ScanSummary:
    scanner_run_id: uuid.UUID
    status: ScannerRunStatus
    detected: int
    stored: int
    error: str | None


def build_command(
    snapshot: Path, output_dir: Path, config: SemgrepConfig = SemgrepConfig()
) -> list[str]:
    """The docker argv for one scan. Argument list only, never a shell string."""
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--memory",
        config.memory_limit,
        "--cpus",
        config.cpu_limit,
        "-v",
        f"{Path(snapshot).resolve().as_posix()}:{SNAPSHOT_MOUNT}:ro",
        "-v",
        f"{Path(output_dir).resolve().as_posix()}:{OUTPUT_MOUNT}",
        config.image,
        "scan",
    ]
    for rules in RULESET_CONFIGS:
        command += ["--config", rules]
    command += [
        # Keep the bundled rule ids as authored instead of prefixing the config path.
        "--no-rewrite-rule-ids",
        "--json",
        "--output",
        f"{OUTPUT_MOUNT}/report.json",
        "--metrics=off",
        "--disable-version-check",
        "--quiet",
        SNAPSHOT_MOUNT,
    ]
    return command


VERSION_LABEL = "com.due-diligence.scanner.version"


def scanner_version(config: SemgrepConfig = SemgrepConfig()) -> str | None:
    """Read the scanner version from the image label (scanner_runs.version).

    Starting a container just to run `--version` costs a full container start, so
    the label baked into the Dockerfile is preferred; the image tag is the fallback.
    """
    try:
        completed = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                config.image,
                "--format",
                f'{{{{index .Config.Labels "{VERSION_LABEL}"}}}}',
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    label = (completed.stdout or "").strip()
    if label and label != "<no value>":
        return label
    tag = config.image.rsplit(":", 1)[-1] if ":" in config.image else ""
    return tag or None


def _relative_path(raw: str) -> str:
    path = raw.replace("\\", "/")
    prefix = f"{SNAPSHOT_MOUNT}/"
    if path.startswith(prefix):
        path = path[len(prefix) :]
    return path.lstrip("/")


def run_semgrep(
    snapshot: Path,
    output_dir: Path,
    *,
    config: SemgrepConfig = SemgrepConfig(),
) -> SemgrepRunResult:
    """Execute the scanner image and parse its JSON report."""
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "report.json"
    report_path.unlink(missing_ok=True)

    try:
        completed = subprocess.run(
            build_command(snapshot, output_dir, config),
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return SemgrepRunResult(
            results=(),
            version=config.image,
            exit_code=-1,
            error=f"semgrep timed out after {config.timeout_seconds}s",
            report_path=None,
        )
    except OSError as exc:
        return SemgrepRunResult(
            results=(),
            version=config.image,
            exit_code=-1,
            error=redact_text(f"could not start semgrep: {exc}")[:MAX_ERROR_CHARS],
            report_path=None,
        )

    stderr = redact_text(completed.stderr or "")[:MAX_ERROR_CHARS]
    if completed.returncode != 0:
        return SemgrepRunResult(
            results=(),
            version=config.image,
            exit_code=completed.returncode,
            error=f"semgrep exited with {completed.returncode}: {stderr}".strip(),
            report_path=None,
        )
    if not report_path.is_file():
        return SemgrepRunResult(
            results=(),
            version=config.image,
            exit_code=completed.returncode,
            error="semgrep produced no report file",
            report_path=None,
        )

    try:
        payload = json.loads(report_path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as exc:
        return SemgrepRunResult(
            results=(),
            version=config.image,
            exit_code=completed.returncode,
            error=redact_text(f"could not parse semgrep report: {exc}"),
            report_path=None,
        )

    parsed_errors = payload.get("errors") or []
    if parsed_errors:
        logger.warning("semgrep reported %d scan errors", len(parsed_errors))

    results = tuple(
        SemgrepResult(
            rule_id=str(item.get("check_id") or "unknown"),
            severity=str((item.get("extra") or {}).get("severity") or "").upper(),
            message=str((item.get("extra") or {}).get("message") or ""),
            file=_relative_path(str(item.get("path") or "")),
            line_start=int((item.get("start") or {}).get("line") or 0),
            line_end=int((item.get("end") or {}).get("line") or 0),
        )
        for item in payload.get("results", [])
    )
    return SemgrepRunResult(
        results=results,
        version=config.image,
        exit_code=completed.returncode,
        error=None,
        report_path=report_path,
    )


def _read_snippet(root: Path, relative_path: str, line_start: int, line_end: int) -> str:
    """Read the cited lines from the snapshot instead of trusting the report.

    Semgrep's JSON `extra.lines` is not guaranteed to contain the code, so the
    snippet is taken from the file the rule pointed at.
    """
    root = Path(root).resolve()
    target = (root / relative_path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return ""
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    start = max(1, line_start)
    end = min(max(line_end, start), len(lines), start + 40)
    return "\n".join(lines[start - 1 : end])


def _title(result: SemgrepResult) -> str:
    first_line = result.message.strip().splitlines()[0] if result.message.strip() else ""
    title = first_line or result.rule_id
    return title[:200]


def scan_audit(
    session: Session,
    audit_id: uuid.UUID,
    snapshot_root: Path,
    *,
    config: SemgrepConfig = SemgrepConfig(),
    output_root: Path | None = None,
) -> ScanSummary:
    """Run semgrep for one audit and store normalized findings and evidence."""
    audit = session.get(Audit, audit_id)
    if audit is None:
        raise ValueError(f"audit {audit_id} does not exist")

    version = scanner_version(config)
    run = ScannerRun(
        audit_id=audit_id,
        tool=ScannerTool.SEMGREP,
        version=version,
        status=ScannerRunStatus.RUNNING,
        started_at=datetime.now(timezone.utc),
    )
    session.add(run)
    session.commit()
    session.refresh(run)

    output_dir = (output_root or Path("./data/scanner-output")) / str(audit_id) / "semgrep"
    result = run_semgrep(snapshot_root, output_dir, config=config)

    if result.error is not None:
        run.status = ScannerRunStatus.FAILED
        run.error = result.error
        run.finished_at = datetime.now(timezone.utc)
        session.commit()
        logger.warning("semgrep failed for audit %s: %s", audit_id, result.error)
        return ScanSummary(
            scanner_run_id=run.id,
            status=ScannerRunStatus.FAILED,
            detected=0,
            stored=0,
            error=result.error,
        )

    existing = existing_fingerprints(session, audit_id)

    stored = 0
    for item in result.results:
        fingerprint = finding_fingerprint(
            category=Category.SECURITY.value,
            rule_id=item.rule_id,
            file_path=item.file,
            line_start=item.line_start,
            line_end=item.line_end,
        )
        if fingerprint in existing:
            continue

        finding = Finding(
            audit_id=audit_id,
            category=Category.SECURITY,
            title=_title(item),
            severity=map_severity(item.severity),
            confidence=Decimal("0.90"),  # rule match, not a calibrated probability
            origin=FindingOrigin.SCANNER,
            source_tool=item.rule_id,
            verification_status=VerificationStatus.SCANNER_CONFIRMED,
            verdict=Verdict.ISSUE,
            reasoning=(
                f"Semgrep rule {item.rule_id} matched {item.file}:"
                f"{item.line_start}-{item.line_end} ({item.severity or 'unknown'} severity)."
            ),
            fingerprint=fingerprint,
        )
        session.add(finding)
        session.flush()  # assign finding.id before the evidence row

        session.add(
            Evidence(
                finding_id=finding.id,
                file_id=None,  # linked to files when indexing has run for this audit
                file_path=item.file,
                line_start=item.line_start,
                line_end=item.line_end,
                snippet=redact_text(
                    _read_snippet(snapshot_root, item.file, item.line_start, item.line_end)
                )[:MAX_SNIPPET_CHARS],
                kind=EvidenceKind.PRIMARY,
            )
        )
        existing.add(fingerprint)
        stored += 1

    run.status = ScannerRunStatus.SUCCEEDED
    run.finished_at = datetime.now(timezone.utc)
    run.raw_output_path = str(result.report_path)
    session.commit()

    logger.info(
        "semgrep finished for audit %s: %d detected, %d stored",
        audit_id,
        len(result.results),
        stored,
    )
    return ScanSummary(
        scanner_run_id=run.id,
        status=ScannerRunStatus.SUCCEEDED,
        detected=len(result.results),
        stored=stored,
        error=None,
    )
