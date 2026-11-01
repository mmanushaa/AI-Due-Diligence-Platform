"""Gitleaks adapter (step 1.4).

Runs the scanner image with subprocess (argument list, no shell, timeout,
network disabled, read-only repository mount), parses its JSON report and
normalizes each leak into the finding schema.

Secret handling: the raw value is never stored or logged. Only a masked
preview, a fingerprint over rule/file/lines, and a redacted report file are
kept.
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
    mask_secret,
    redact_text,
)

logger = logging.getLogger(__name__)

GITLEAKS_IMAGE = "due-diligence-gitleaks:8.28.0"
SNAPSHOT_MOUNT = "/repo"
OUTPUT_MOUNT = "/out"
@dataclass(frozen=True)
class GitleaksConfig:
    image: str = GITLEAKS_IMAGE
    timeout_seconds: int = 300
    memory_limit: str = "512m"
    cpu_limit: str = "1"


@dataclass(frozen=True)
class GitleaksLeak:
    rule_id: str
    description: str
    file: str
    line_start: int
    line_end: int
    secret: str
    match: str


@dataclass(frozen=True)
class GitleaksRunResult:
    leaks: tuple[GitleaksLeak, ...]
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


def _relative_path(raw: str) -> str:
    path = raw.replace("\\", "/")
    prefix = f"{SNAPSHOT_MOUNT}/"
    if path.startswith(prefix):
        path = path[len(prefix) :]
    return path.lstrip("/")


def build_command(
    snapshot: Path, output_dir: Path, config: GitleaksConfig = GitleaksConfig()
) -> list[str]:
    """The docker argv for one scan. Argument list only, never a shell string."""
    return [
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
        "dir",
        SNAPSHOT_MOUNT,
        "--report-format",
        "json",
        "--report-path",
        f"{OUTPUT_MOUNT}/report.json",
        "--exit-code",
        "0",
        "--log-level",
        "error",
    ]


def scanner_version(config: GitleaksConfig = GitleaksConfig()) -> str | None:
    """Ask the image for its version; used for scanner_runs.version."""
    try:
        completed = subprocess.run(
            ["docker", "run", "--rm", config.image, "version"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (completed.stdout or "").strip().splitlines()
    return output[0].strip() if output else None


def run_gitleaks(
    snapshot: Path,
    output_dir: Path,
    *,
    config: GitleaksConfig = GitleaksConfig(),
) -> GitleaksRunResult:
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
        return GitleaksRunResult(
            leaks=(),
            version=config.image,
            exit_code=-1,
            error=f"gitleaks timed out after {config.timeout_seconds}s",
            report_path=None,
        )
    except OSError as exc:
        return GitleaksRunResult(
            leaks=(),
            version=config.image,
            exit_code=-1,
            error=redact_text(f"could not start gitleaks: {exc}")[:MAX_ERROR_CHARS],
            report_path=None,
        )

    stderr = redact_text(completed.stderr or "")[:MAX_ERROR_CHARS]
    if completed.returncode != 0:
        return GitleaksRunResult(
            leaks=(),
            version=config.image,
            exit_code=completed.returncode,
            error=f"gitleaks exited with {completed.returncode}: {stderr}".strip(),
            report_path=None,
        )
    if not report_path.is_file():
        return GitleaksRunResult(
            leaks=(),
            version=config.image,
            exit_code=completed.returncode,
            error="gitleaks produced no report file",
            report_path=None,
        )

    try:
        raw = json.loads(report_path.read_text(encoding="utf-8") or "[]")
    except json.JSONDecodeError as exc:
        return GitleaksRunResult(
            leaks=(),
            version=config.image,
            exit_code=completed.returncode,
            error=redact_text(f"could not parse gitleaks report: {exc}"),
            report_path=None,
        )

    leaks = tuple(
        GitleaksLeak(
            rule_id=str(item.get("RuleID") or "unknown"),
            description=str(item.get("Description") or item.get("RuleID") or "Secret"),
            file=_relative_path(str(item.get("File") or "")),
            line_start=int(item.get("StartLine") or 0),
            line_end=int(item.get("EndLine") or item.get("StartLine") or 0),
            secret=str(item.get("Secret") or ""),
            match=str(item.get("Match") or ""),
        )
        for item in raw
    )
    return GitleaksRunResult(
        leaks=leaks,
        version=config.image,
        exit_code=completed.returncode,
        error=None,
        report_path=report_path,
    )


def _redacted_report(leaks: tuple[GitleaksLeak, ...]) -> str:
    """The report we are willing to keep on disk: secrets replaced by previews."""
    payload = [
        {
            "RuleID": leak.rule_id,
            "Description": leak.description,
            "File": leak.file,
            "StartLine": leak.line_start,
            "EndLine": leak.line_end,
            "Secret": mask_secret(leak.secret),
            "Match": redact_text(leak.match, leak.secret)[:MAX_SNIPPET_CHARS],
        }
        for leak in leaks
    ]
    return json.dumps(payload, indent=2)


def scan_audit(
    session: Session,
    audit_id: uuid.UUID,
    snapshot_root: Path,
    *,
    config: GitleaksConfig = GitleaksConfig(),
    output_root: Path | None = None,
) -> ScanSummary:
    """Run gitleaks for one audit and store normalized findings and evidence."""
    audit = session.get(Audit, audit_id)
    if audit is None:
        raise ValueError(f"audit {audit_id} does not exist")

    version = scanner_version(config)
    run = ScannerRun(
        audit_id=audit_id,
        tool=ScannerTool.GITLEAKS,
        version=version,
        status=ScannerRunStatus.RUNNING,
        started_at=datetime.now(timezone.utc),
    )
    session.add(run)
    session.commit()
    session.refresh(run)

    output_dir = (output_root or Path("./data/scanner-output")) / str(audit_id) / "gitleaks"
    result = run_gitleaks(snapshot_root, output_dir, config=config)

    if result.error is not None:
        run.status = ScannerRunStatus.FAILED
        run.error = result.error
        run.finished_at = datetime.now(timezone.utc)
        session.commit()
        logger.warning(
            "gitleaks failed for audit %s: %s", audit_id, result.error
        )
        return ScanSummary(
            scanner_run_id=run.id,
            status=ScannerRunStatus.FAILED,
            detected=0,
            stored=0,
            error=result.error,
        )

    redacted_path = output_dir / "report.redacted.json"
    redacted_path.write_text(_redacted_report(result.leaks), encoding="utf-8")

    existing = existing_fingerprints(session, audit_id)

    stored = 0
    for leak in result.leaks:
        fingerprint = finding_fingerprint(
            category=Category.SECRET.value,
            rule_id=leak.rule_id,
            file_path=leak.file,
            line_start=leak.line_start,
            line_end=leak.line_end,
        )
        if fingerprint in existing:
            continue

        finding = Finding(
            audit_id=audit_id,
            category=Category.SECRET,
            title=f"{leak.description} detected by gitleaks",
            severity=Severity.HIGH,
            confidence=Decimal("0.90"),  # scanner match, not a calibrated probability
            origin=FindingOrigin.SCANNER,
            source_tool=f"gitleaks:{leak.rule_id}",
            verification_status=VerificationStatus.SCANNER_CONFIRMED,
            verdict=Verdict.ISSUE,
            reasoning=(
                f"Gitleaks rule {leak.rule_id} matched {leak.file}:"
                f"{leak.line_start}-{leak.line_end}. The value is masked in storage."
            ),
            business_impact="A committed credential can grant access to the systems it unlocks.",
            recommendation="Remove the secret from the repository, rotate it, and load it from the environment.",
            fingerprint=fingerprint,
        )
        session.add(finding)
        session.flush()  # assign finding.id before the evidence row

        session.add(
            Evidence(
                finding_id=finding.id,
                file_id=None,  # linked to files when indexing has run for this audit
                file_path=leak.file,
                line_start=leak.line_start,
                line_end=leak.line_end,
                snippet=redact_text(leak.match, leak.secret)[:MAX_SNIPPET_CHARS],
                kind=EvidenceKind.PRIMARY,
            )
        )
        existing.add(fingerprint)
        stored += 1

    run.status = ScannerRunStatus.SUCCEEDED
    run.finished_at = datetime.now(timezone.utc)
    run.raw_output_path = str(redacted_path)
    session.commit()

    logger.info(
        "gitleaks finished for audit %s: %d detected, %d stored", audit_id, len(result.leaks), stored
    )
    return ScanSummary(
        scanner_run_id=run.id,
        status=ScannerRunStatus.SUCCEEDED,
        detected=len(result.leaks),
        stored=stored,
        error=None,
    )
