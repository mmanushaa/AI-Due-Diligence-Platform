"""Scanner orchestration (step 1.8).

Runs the four scanners against one snapshot in parallel and records what happened to
each. A scanner is isolated in two ways: it gets its own database session and its own
container/process, and a failure is contained to its own `scanner_runs` row — the
audit continues and the other scanners' findings are untouched.

Dedupe is by fingerprint: each adapter only inserts a finding whose
``(category, rule, file, line range)`` hash is not already stored for the audit, so
running the same audit twice stores nothing new.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.enums import ScannerRunStatus, ScannerTool
from app.db.models import ScannerRun
from app.db.session import get_session_factory
from app.scanners import gitleaks, osv, scancode, semgrep
from app.scanners.common import MAX_ERROR_CHARS, redact_text

logger = logging.getLogger(__name__)

# Order is fixed so reports and tests read the same every run.
SCANNER_ORDER = (
    ScannerTool.GITLEAKS,
    ScannerTool.SEMGREP,
    ScannerTool.OSV,
    ScannerTool.SCANCODE,
)

ScanFunction = Callable[..., object]


@dataclass(frozen=True)
class ScannerConfigs:
    """Per-scanner settings. Each scanner keeps its own default."""

    gitleaks: gitleaks.GitleaksConfig = gitleaks.GitleaksConfig()
    semgrep: semgrep.SemgrepConfig = semgrep.SemgrepConfig()
    osv: osv.OsvConfig = osv.OsvConfig()
    scancode: scancode.ScancodeConfig = scancode.ScancodeConfig()


DEFAULT_SCANNERS: Mapping[ScannerTool, ScanFunction] = {
    ScannerTool.GITLEAKS: gitleaks.scan_audit,
    ScannerTool.SEMGREP: semgrep.scan_audit,
    ScannerTool.OSV: osv.scan_audit,
    ScannerTool.SCANCODE: scancode.scan_audit,
}


@dataclass(frozen=True)
class ScannerOutcome:
    tool: ScannerTool
    status: ScannerRunStatus
    detected: int = 0
    stored: int = 0
    error: str | None = None
    scanner_run_id: uuid.UUID | None = None

    @property
    def ok(self) -> bool:
        return self.status is ScannerRunStatus.SUCCEEDED


@dataclass(frozen=True)
class OrchestrationResult:
    outcomes: tuple[ScannerOutcome, ...]

    @property
    def failed(self) -> tuple[ScannerOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if not outcome.ok)

    @property
    def all_succeeded(self) -> bool:
        return not self.failed


def _config_for(tool: ScannerTool, configs: ScannerConfigs) -> object:
    return getattr(configs, tool.value)


def _record_failure(
    session_factory: sessionmaker[Session], audit_id: uuid.UUID, tool: ScannerTool, error: str
) -> uuid.UUID | None:
    """Mark this scanner's run failed, creating one if the adapter never got that far."""
    now = datetime.now(timezone.utc)
    try:
        with session_factory() as session:
            run = session.execute(
                select(ScannerRun)
                .where(ScannerRun.audit_id == audit_id, ScannerRun.tool == tool)
                .order_by(ScannerRun.created_at.desc())
                .limit(1)
            ).scalars().first()

            if run is not None and run.status in (
                ScannerRunStatus.PENDING,
                ScannerRunStatus.RUNNING,
            ):
                run.status = ScannerRunStatus.FAILED
                run.error = error
                run.finished_at = now
            else:
                # A failure before the adapter opened its row (a bad image name at
                # import time, a missing audit) still has to leave a failed run.
                run = ScannerRun(
                    audit_id=audit_id,
                    tool=tool,
                    status=ScannerRunStatus.FAILED,
                    error=error,
                    started_at=now,
                    finished_at=now,
                )
                session.add(run)
            session.commit()
            session.refresh(run)
            return run.id
    except Exception:  # noqa: BLE001 - reporting a failure must not raise a second one
        logger.exception("could not record the failed %s run for audit %s", tool.value, audit_id)
        return None


def _execute(
    tool: ScannerTool,
    scan: ScanFunction,
    config: object,
    audit_id: uuid.UUID,
    snapshot_root: Path,
    output_root: Path | None,
    session_factory: sessionmaker[Session],
) -> ScannerOutcome:
    """Run one scanner in its own session and turn any exception into a failed row."""
    session = session_factory()
    try:
        summary = scan(
            session,
            audit_id,
            snapshot_root,
            config=config,
            output_root=output_root,
        )
        return ScannerOutcome(
            tool=tool,
            status=summary.status,
            detected=summary.detected,
            stored=summary.stored,
            error=summary.error,
            scanner_run_id=summary.scanner_run_id,
        )
    except Exception as exc:  # noqa: BLE001 - one scanner must never sink the others
        error = redact_text(f"{type(exc).__name__}: {exc}")[:MAX_ERROR_CHARS]
        logger.warning("scanner %s failed for audit %s: %s", tool.value, audit_id, error)
        return ScannerOutcome(
            tool=tool,
            status=ScannerRunStatus.FAILED,
            error=error,
            scanner_run_id=_record_failure(session_factory, audit_id, tool, error),
        )
    finally:
        session.close()


def run_scanners(
    audit_id: uuid.UUID,
    snapshot_root: Path | str,
    *,
    configs: ScannerConfigs | None = None,
    scanners: Mapping[ScannerTool, ScanFunction] | None = None,
    output_root: Path | str | None = None,
    session_factory: sessionmaker[Session] | None = None,
    max_workers: int | None = None,
) -> OrchestrationResult:
    """Run every scanner for one audit in parallel and report each outcome."""
    configs = configs or ScannerConfigs()
    selected = dict(scanners or DEFAULT_SCANNERS)
    session_factory = session_factory or get_session_factory()
    snapshot = Path(snapshot_root)
    output = Path(output_root) if output_root is not None else None

    ordered = [tool for tool in SCANNER_ORDER if tool in selected]
    ordered += [tool for tool in selected if tool not in SCANNER_ORDER]
    workers = max(1, min(len(ordered), max_workers or len(ordered)))

    outcomes: dict[ScannerTool, ScannerOutcome] = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scanner") as pool:
        futures = {
            pool.submit(
                _execute,
                tool,
                selected[tool],
                _config_for(tool, configs),
                audit_id,
                snapshot,
                output,
                session_factory,
            ): tool
            for tool in ordered
        }
        for future in as_completed(futures):
            tool = futures[future]
            try:
                outcomes[tool] = future.result()
            except Exception as exc:  # noqa: BLE001 - defensive: _execute already catches
                error = redact_text(f"{type(exc).__name__}: {exc}")[:MAX_ERROR_CHARS]
                outcomes[tool] = ScannerOutcome(
                    tool=tool,
                    status=ScannerRunStatus.FAILED,
                    error=error,
                    scanner_run_id=_record_failure(session_factory, audit_id, tool, error),
                )

    result = OrchestrationResult(outcomes=tuple(outcomes[tool] for tool in ordered))
    logger.info(
        "scanners finished for audit %s: %d succeeded, %d failed",
        audit_id,
        len(result.outcomes) - len(result.failed),
        len(result.failed),
    )
    return result
