"""OSV-Scanner adapter (step 1.6).

The scanner image carries the OSV vulnerability database for its ecosystems, so
the scan runs with ``--network none`` (offline mode). The adapter executes it
with subprocess (argument list, no shell, timeout, read-only mounts) and
normalizes each advisory group into the finding schema.

A bare ``package.json`` is not a source OSV-Scanner understands, so for npm
manifests without a lockfile the adapter writes a temporary package-lock.json
containing the exactly pinned versions. That file lives under the scanner
output directory, never inside the snapshot, and nothing from the repository is
executed.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
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
    finding_fingerprint,
    redact_text,
)

logger = logging.getLogger(__name__)

OSV_IMAGE = "due-diligence-osv:2.7.0"
SNAPSHOT_MOUNT = "/repo"
MANIFEST_MOUNT = "/manifests"
OUTPUT_MOUNT = "/out"
VERSION_LABEL = "com.due-diligence.scanner.version"

# Directories that hold installed dependencies or build output, never sources.
SKIP_DIRECTORIES = {
    ".git",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
}

NPM_LOCKFILES = ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml")

# GHSA qualitative ratings.
GHSA_SEVERITY = {
    "CRITICAL": Severity.CRITICAL,
    "HIGH": Severity.HIGH,
    "MODERATE": Severity.MEDIUM,
    "MEDIUM": Severity.MEDIUM,
    "LOW": Severity.LOW,
}
DEFAULT_SEVERITY = Severity.MEDIUM

EXACT_VERSION = re.compile(r"^=?v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$")


@dataclass(frozen=True)
class OsvConfig:
    image: str = OSV_IMAGE
    timeout_seconds: int = 600
    memory_limit: str = "2g"
    cpu_limit: str = "2"


@dataclass(frozen=True)
class PackageVulnerability:
    """One advisory group for one package, before it reaches the database."""

    ecosystem: str
    package: str
    version: str
    advisory_id: str
    aliases: tuple[str, ...]
    summary: str
    severity: Severity
    cvss_score: float | None
    fixed_version: str | None
    manifest_path: str
    line_start: int
    line_end: int
    snippet: str


@dataclass(frozen=True)
class OsvRunResult:
    vulnerabilities: tuple[PackageVulnerability, ...]
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


# --- severity ---------------------------------------------------------------


def map_severity(value: str | None, cvss_score: float | None = None) -> Severity:
    """GHSA's qualitative rating first, then the CVSS score, then a default."""
    if value:
        mapped = GHSA_SEVERITY.get(value.strip().upper())
        if mapped is not None:
            return mapped
    if cvss_score is not None:
        if cvss_score >= 9.0:
            return Severity.CRITICAL
        if cvss_score >= 7.0:
            return Severity.HIGH
        if cvss_score >= 4.0:
            return Severity.MEDIUM
        if cvss_score > 0:
            return Severity.LOW
    return DEFAULT_SEVERITY


# --- versions ---------------------------------------------------------------


def _version_key(version: str) -> tuple:
    """Sortable form that tolerates pre-release and non-numeric segments."""
    parts: list[tuple[int, object]] = []
    for token in re.split(r"[.\-+]", version.strip().lstrip("v")):
        if token.isdigit():
            parts.append((0, int(token)))
        elif token:
            parts.append((1, token))
    return tuple(parts)


def _fixed_versions(payload: dict, ecosystem: str, package: str) -> list[str]:
    fixed: list[str] = []
    for affected in payload.get("affected") or []:
        target = affected.get("package") or {}
        if str(target.get("ecosystem")) != ecosystem or str(target.get("name")) != package:
            continue
        for version_range in affected.get("ranges") or []:
            for event in version_range.get("events") or []:
                if event.get("fixed"):
                    fixed.append(str(event["fixed"]))
    return fixed


def _lowest_fixed_after(fixed: list[str], installed: str) -> str | None:
    current = _version_key(installed)
    newer = [candidate for candidate in fixed if _version_key(candidate) > current]
    if not newer:
        return None
    return min(newer, key=_version_key)


# --- manifests --------------------------------------------------------------


def _normalize_package(name: str, ecosystem: str) -> str:
    lowered = name.strip().lower()
    if ecosystem.lower() == "pypi":
        return re.sub(r"[-_.]+", "-", lowered)
    return lowered


def _pin(value: object) -> str | None:
    """Only exact pins are usable offline; ranges say nothing about the version."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if candidate.startswith("npm:") or candidate.startswith("workspace:"):
        return None
    if EXACT_VERSION.match(candidate):
        return candidate.lstrip("=").lstrip("v")
    return None


def _npm_dependencies(manifest: Path) -> dict[str, str]:
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    pinned: dict[str, str] = {}
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        block = payload.get(section)
        if not isinstance(block, dict):
            continue
        for name, spec in block.items():
            version = _pin(spec)
            if version is not None:
                pinned[name] = version
    return pinned


def write_synthetic_npm_lockfiles(snapshot: Path, target: Path) -> dict[str, str]:
    """Create minimal lockfiles for package.json files that have no lockfile.

    Returns a mapping from the synthetic lockfile (relative to ``target``) to the
    manifest it was built from (relative to ``snapshot``). Versions that are
    ranges (``^4.17.19``) are skipped: the installed version is unknown.
    """
    mapping: dict[str, str] = {}
    snapshot = Path(snapshot)
    for manifest in sorted(snapshot.rglob("package.json")):
        relative = manifest.relative_to(snapshot)
        if any(part in SKIP_DIRECTORIES for part in relative.parts[:-1]):
            continue
        if any((manifest.parent / name).exists() for name in NPM_LOCKFILES):
            continue
        pinned = _npm_dependencies(manifest)
        if not pinned:
            continue

        lock_dir = target / relative.parent
        lock_dir.mkdir(parents=True, exist_ok=True)
        packages: dict[str, object] = {
            "": {"name": relative.parent.as_posix() or "root", "dependencies": pinned}
        }
        for name, version in pinned.items():
            packages[f"node_modules/{name}"] = {
                "version": version,
                "resolved": f"https://registry.npmjs.org/{name}/-/{name.split('/')[-1]}-{version}.tgz",
            }
        lockfile = lock_dir / "package-lock.json"
        lockfile.write_text(
            json.dumps(
                {
                    "name": relative.parent.as_posix() or "root",
                    "version": "0.0.0",
                    "lockfileVersion": 3,
                    "requires": True,
                    "packages": packages,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        mapping[lockfile.relative_to(target).as_posix()] = relative.as_posix()
    return mapping


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def find_manifest_line(path: Path, package: str, ecosystem: str) -> tuple[int, str]:
    """Locate the line where a dependency is declared or resolved.

    Falls back to line 1 when nothing matches, so evidence always points at the
    file that named the package rather than a fabricated location.
    """
    lines = _read_lines(path)
    name = _normalize_package(package, ecosystem)
    suffixes = (".txt", ".in")
    if path.name.lower().endswith(suffixes):
        for number, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("-"):
                continue
            declared = re.split(r"[<>=!~;\[ ]", stripped, maxsplit=1)[0]
            if _normalize_package(declared, ecosystem) == name:
                return number, stripped[:MAX_SNIPPET_CHARS]
        return 1, ""

    if path.name == "package.json":
        needle = re.compile(rf'^\s*"{re.escape(package)}"\s*:')
        for number, line in enumerate(lines, start=1):
            if needle.match(line):
                return number, line.strip()[:MAX_SNIPPET_CHARS]
        return 1, ""

    if path.name in {"package-lock.json", "npm-shrinkwrap.json"}:
        needles = (
            re.compile(rf'^\s*"node_modules/{re.escape(package)}"\s*:'),
            re.compile(rf'^\s*"{re.escape(package)}"\s*:'),
        )
        for number, line in enumerate(lines, start=1):
            if any(pattern.match(line) for pattern in needles):
                return number, line.strip()[:MAX_SNIPPET_CHARS]
        return 1, ""

    if path.name == "yarn.lock":
        for number, line in enumerate(lines, start=1):
            if line.startswith(f'"{package}@') or line.startswith(f"{package}@"):
                return number, line.strip()[:MAX_SNIPPET_CHARS]
        return 1, ""

    for number, line in enumerate(lines, start=1):
        if package in line:
            return number, line.strip()[:MAX_SNIPPET_CHARS]
    return 1, ""


def _strip_mount(path: str, mount: str) -> str | None:
    """Turn `/repo/app/x.txt` into `app/x.txt`; None when another mount owns it."""
    name = mount.strip("/")
    if path == name:
        return ""
    if path.startswith(name + "/"):
        return path[len(name) + 1 :]
    return None


def _manifest_for(
    source: str, snapshot: Path, synthetic: dict[str, str]
) -> tuple[str, Path]:
    """Map an OSV result source back to a repository-relative manifest and file."""
    path = source.replace("\\", "/").lstrip("/")
    relative_lock = _strip_mount(path, MANIFEST_MOUNT)
    if relative_lock is not None:
        manifest_relative = synthetic.get(relative_lock, relative_lock)
        return manifest_relative, snapshot / manifest_relative
    repository_path = _strip_mount(path, SNAPSHOT_MOUNT)
    if repository_path is not None:
        path = repository_path
    return path, snapshot / path


def _preferred_manifest(manifest_file: Path, snapshot: Path) -> tuple[str, Path]:
    """Prefer the human-written manifest over a lockfile OSV reported."""
    if manifest_file.name in NPM_LOCKFILES:
        sibling = manifest_file.with_name("package.json")
        if sibling.is_file():
            return sibling.relative_to(snapshot).as_posix(), sibling
    return manifest_file.relative_to(snapshot).as_posix(), manifest_file


# --- docker -----------------------------------------------------------------


def build_command(
    snapshot: Path,
    output_dir: Path,
    *,
    manifest_dir: Path | None = None,
    config: OsvConfig = OsvConfig(),
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
    ]
    roots = [SNAPSHOT_MOUNT]
    if manifest_dir is not None:
        command += [
            "-v",
            f"{Path(manifest_dir).resolve().as_posix()}:{MANIFEST_MOUNT}:ro",
        ]
        roots.append(MANIFEST_MOUNT)
    command += [config.image, "scan", "source", "--offline", "--offline-vulnerabilities"]
    command += ["--format", "json", "--output-file", f"{OUTPUT_MOUNT}/report.json"]
    command += ["--verbosity", "error", "-r", *roots]
    return command


def scanner_version(config: OsvConfig = OsvConfig()) -> str | None:
    """Read the version from the image label; no container start needed."""
    try:
        completed = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                f"{{{{ index .Config.Labels \"{VERSION_LABEL}\" }}}}",
                config.image,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    label = (completed.stdout or "").strip()
    if completed.returncode != 0 or not label or label == "<no value>":
        return None
    return label


# --- scan -------------------------------------------------------------------


def run_osv(
    snapshot: Path,
    output_dir: Path,
    *,
    manifest_dir: Path | None = None,
    config: OsvConfig = OsvConfig(),
    synthetic: dict[str, str] | None = None,
) -> OsvRunResult:
    """Execute the scanner image and parse its JSON report."""
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "report.json"
    report_path.unlink(missing_ok=True)

    try:
        completed = subprocess.run(
            build_command(snapshot, output_dir, manifest_dir=manifest_dir, config=config),
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return OsvRunResult(
            vulnerabilities=(),
            version=None,
            exit_code=-1,
            error=f"osv-scanner timed out after {config.timeout_seconds}s",
            report_path=None,
        )
    except OSError as exc:
        return OsvRunResult(
            vulnerabilities=(),
            version=None,
            exit_code=-1,
            error=redact_text(f"could not start osv-scanner: {exc}")[:MAX_ERROR_CHARS],
            report_path=None,
        )

    stderr = redact_text(completed.stderr or "")[:MAX_ERROR_CHARS]
    # 0 = clean, 1 = vulnerabilities found; anything else is a scanner failure.
    if completed.returncode not in (0, 1):
        return OsvRunResult(
            vulnerabilities=(),
            version=None,
            exit_code=completed.returncode,
            error=f"osv-scanner exited with {completed.returncode}: {stderr}".strip(),
            report_path=None,
        )
    if not report_path.is_file():
        return OsvRunResult(
            vulnerabilities=(),
            version=None,
            exit_code=completed.returncode,
            error="osv-scanner produced no report file",
            report_path=None,
        )

    try:
        payload = json.loads(report_path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as exc:
        return OsvRunResult(
            vulnerabilities=(),
            version=None,
            exit_code=completed.returncode,
            error=redact_text(f"could not parse osv-scanner report: {exc}")[:MAX_ERROR_CHARS],
            report_path=None,
        )

    synthetic = synthetic or {}
    found: list[PackageVulnerability] = []
    for result in payload.get("results", []) or []:
        source = str((result.get("source") or {}).get("path") or "")
        manifest_relative, manifest_file = _manifest_for(source, snapshot, synthetic)
        for entry in result.get("packages", []) or []:
            package = entry.get("package") or {}
            name = str(package.get("name") or "")
            version = str(package.get("version") or "")
            ecosystem = str(package.get("ecosystem") or "")
            if not name or not version or not ecosystem:
                continue
            by_id = {
                str(item.get("id")): item for item in entry.get("vulnerabilities") or []
            }
            for group in entry.get("groups") or []:
                ids = [str(item) for item in group.get("ids") or []]
                aliases = [str(item) for item in group.get("aliases") or []]
                advisories = by_id.get(ids[0]) if ids else None
                if advisories is None:
                    continue
                asked = [item for item in ids + aliases if item.startswith("GHSA-")]
                advisory_id = asked[0] if asked else (ids[0] if ids else "unknown")
                # The group's first id can be a PYSEC record without a summary;
                # prefer the record we name in source_tool.
                record = by_id.get(advisory_id) or advisories
                score: float | None = None
                try:
                    if group.get("max_severity") is not None:
                        score = float(group["max_severity"])
                except (TypeError, ValueError):
                    score = None
                database_specific = record.get("database_specific") or {}
                installed = _lowest_fixed_after(
                    _fixed_versions(record, ecosystem, name), version
                )

                target_relative, target_file = _preferred_manifest(manifest_file, snapshot)
                line, snippet = find_manifest_line(target_file, name, ecosystem)
                if line == 1 and not snippet:
                    line, snippet = find_manifest_line(manifest_file, name, ecosystem)

                found.append(
                    PackageVulnerability(
                        ecosystem=ecosystem,
                        package=name,
                        version=version,
                        advisory_id=advisory_id,
                        aliases=tuple(dict.fromkeys(ids + aliases)),
                        summary=str(record.get("summary") or "known vulnerability"),
                        severity=map_severity(database_specific.get("severity"), score),
                        cvss_score=score,
                        fixed_version=installed,
                        manifest_path=target_relative,
                        line_start=line,
                        line_end=line,
                        snippet=snippet,
                    )
                )

    return OsvRunResult(
        vulnerabilities=tuple(found),
        version=None,
        exit_code=completed.returncode,
        error=None,
        report_path=report_path,
    )


# --- persistence ------------------------------------------------------------


def scan_audit(
    session: Session,
    audit_id: uuid.UUID,
    snapshot_root: Path,
    *,
    config: OsvConfig = OsvConfig(),
    output_root: Path | None = None,
) -> ScanSummary:
    """Run osv-scanner for one audit and store normalized findings and evidence."""
    audit = session.get(Audit, audit_id)
    if audit is None:
        raise ValueError(f"audit {audit_id} does not exist")

    run = ScannerRun(
        audit_id=audit_id,
        tool=ScannerTool.OSV,
        version=scanner_version(config),
        status=ScannerRunStatus.RUNNING,
        started_at=datetime.now(timezone.utc),
    )
    session.add(run)
    session.commit()
    session.refresh(run)

    output_dir = (output_root or Path("./data/scanner-output")) / str(audit_id) / "osv"
    manifest_dir = output_dir / "npm-lockfiles"
    if manifest_dir.exists():
        shutil.rmtree(manifest_dir)
    synthetic = write_synthetic_npm_lockfiles(snapshot_root, manifest_dir)

    result = run_osv(
        snapshot_root,
        output_dir,
        manifest_dir=manifest_dir if synthetic else None,
        config=config,
        synthetic=synthetic,
    )

    if result.error is not None:
        run.status = ScannerRunStatus.FAILED
        run.error = result.error
        run.finished_at = datetime.now(timezone.utc)
        session.commit()
        logger.warning("osv-scanner failed for audit %s: %s", audit_id, result.error)
        return ScanSummary(
            scanner_run_id=run.id,
            status=ScannerRunStatus.FAILED,
            detected=0,
            stored=0,
            error=result.error,
        )

    existing = set(
        session.execute(
            select(Finding.fingerprint).where(Finding.audit_id == audit_id)
        ).scalars()
    )

    stored = 0
    for item in result.vulnerabilities:
        fingerprint = finding_fingerprint(
            category=Category.DEPENDENCY.value,
            rule_id=item.advisory_id,
            file_path=item.manifest_path,
            line_start=item.line_start,
            line_end=item.line_end,
        )
        if fingerprint in existing:
            continue

        if item.fixed_version:
            fix = f"Upgrade {item.package} to {item.fixed_version} or later."
        else:
            fix = (
                f"No fixed version is published for {item.package} {item.version};"
                " replace or isolate the dependency."
            )
        aliases = ", ".join(alias for alias in item.aliases if alias != item.advisory_id)

        finding = Finding(
            audit_id=audit_id,
            category=Category.DEPENDENCY,
            title=f"{item.package} {item.version}: {item.summary}"[:300],
            severity=item.severity,
            confidence=Decimal("0.95"),  # published advisory matched to a pinned version
            origin=FindingOrigin.SCANNER,
            source_tool=f"osv:{item.advisory_id}",
            verification_status=VerificationStatus.SCANNER_CONFIRMED,
            verdict=Verdict.ISSUE,
            reasoning=(
                f"OSV advisory {item.advisory_id}"
                f"{f' (aliases: {aliases})' if aliases else ''}"
                f" affects {item.ecosystem} package {item.package} {item.version}"
                f"{f' (CVSS {item.cvss_score})' if item.cvss_score is not None else ''}."
                f" Declared in {item.manifest_path}:{item.line_start}."
            ),
            business_impact=(
                "A dependency with a published vulnerability can expose the product to"
                " the documented attack; the fixed version is usually a low-risk upgrade."
            ),
            recommendation=f"{fix} Advisory: https://osv.dev/{item.advisory_id}",
            fingerprint=fingerprint,
        )
        session.add(finding)
        session.flush()  # assign finding.id before the evidence row

        session.add(
            Evidence(
                finding_id=finding.id,
                file_id=None,  # linked to files when indexing has run for this audit
                file_path=item.manifest_path,
                line_start=item.line_start,
                line_end=item.line_end,
                snippet=item.snippet or None,
                kind=EvidenceKind.PRIMARY,
            )
        )
        existing.add(fingerprint)
        stored += 1

    run.status = ScannerRunStatus.SUCCEEDED
    run.version = scanner_version(config)
    run.finished_at = datetime.now(timezone.utc)
    run.raw_output_path = str(result.report_path)
    session.commit()

    logger.info(
        "osv-scanner finished for audit %s: %d detected, %d stored",
        audit_id,
        len(result.vulnerabilities),
        stored,
    )
    return ScanSummary(
        scanner_run_id=run.id,
        status=ScannerRunStatus.SUCCEEDED,
        detected=len(result.vulnerabilities),
        stored=stored,
        error=None,
    )
