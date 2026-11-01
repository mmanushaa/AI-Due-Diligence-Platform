"""ScanCode Toolkit adapter (step 1.7).

ScanCode is the license inventory: it detects license text and SPDX identifiers in
the snapshot and lists the packages and dependencies the repository declares. The
scan itself needs no network (the license rules ship in the toolkit wheel), so it
runs with ``--network none`` like the other scanners.

ScanCode cannot know the license of a dependency that is only *named* in a
manifest: licenses live in registry metadata, not in the audited files. For those
the adapter looks the declared license up in the PyPI or npm registry, which is
the one place this step touches the network. That lookup is configurable
(``allow_network``), only ever queries a fixed https origin with a validated
package name and pinned version, and never executes anything from the snapshot.

Every dependency ends in exactly one of three states: a restrictive license
(finding, verdict ``issue``), a permissive license the policy lists (no finding),
or no usable license data (finding, verdict ``insufficient_evidence``). Missing
data is never treated as compliant.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

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

SCANCODE_IMAGE = "due-diligence-scancode:32.5.0"
SNAPSHOT_MOUNT = "/repo"
OUTPUT_MOUNT = "/out"
VERSION_LABEL = "com.due-diligence.scanner.version"

POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "license_policy.json"

# LICENSE_SEVERITY_ORDER keeps the worst restrictive family when an expression
# matches several (for example "GPL-2.0-only OR AGPL-3.0-only").
SEVERITY_ORDER = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}

# Registry lookups are limited to these two origins, and only package names and
# versions that look like plain identifiers are ever put in a URL.
PYPI_ORIGIN = "https://pypi.org"
NPM_ORIGIN = "https://registry.npmjs.org"
MAX_METADATA_BYTES = 2_000_000
PYPI_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
NPM_NAME = re.compile(r"^(?:@[A-Za-z0-9][A-Za-z0-9._-]*/)?[A-Za-z0-9][A-Za-z0-9._-]*$")
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+~-]{0,63}$")
PURL_PATTERN = re.compile(r"^pkg:(?P<type>[^/]+)/(?P<name>[^@?#]+)(?:@(?P<version>[^?#]+))?")
# An exact pin is the only requirement that names one installed version.
EXACT_VERSION = re.compile(r"^=?v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$")


@dataclass(frozen=True)
class ScancodeConfig:
    image: str = SCANCODE_IMAGE
    timeout_seconds: int = 900
    memory_limit: str = "2g"
    cpu_limit: str = "2"
    processes: int = 2
    # Dependency licenses come from PyPI/npm metadata, which needs the network.
    allow_network: bool = True
    metadata_timeout_seconds: int = 15
    policy_path: Path = POLICY_PATH


# --- policy -----------------------------------------------------------------


@dataclass(frozen=True)
class LicenseFamily:
    name: str
    restrictive: bool
    severity: Severity | None
    summary: str | None
    patterns: tuple[re.Pattern[str], ...]


@dataclass(frozen=True)
class LicensePolicy:
    default_severity: Severity
    restrictive: tuple[LicenseFamily, ...]
    permissive: tuple[LicenseFamily, ...]


@dataclass(frozen=True)
class LicenseClassification:
    """What the policy says about one license expression."""

    expression: str
    restrictive: bool
    covered: bool  # every part of the expression matched the policy
    family: str | None
    severity: Severity | None
    summary: str | None


def load_policy(path: Path = POLICY_PATH) -> LicensePolicy:
    """Read the license policy file (restrictive and permissive families)."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))

    def families(key: str, restrictive: bool) -> tuple[LicenseFamily, ...]:
        result = []
        for entry in payload.get(key, []) or []:
            severity = entry.get("severity")
            result.append(
                LicenseFamily(
                    name=str(entry["name"]),
                    restrictive=restrictive,
                    severity=Severity(severity) if severity else None,
                    summary=entry.get("summary"),
                    patterns=tuple(re.compile(p, re.IGNORECASE) for p in entry["patterns"]),
                )
            )
        return tuple(result)

    return LicensePolicy(
        default_severity=Severity(payload.get("default_severity", "medium")),
        restrictive=families("restrictive", True),
        permissive=families("permissive", False),
    )


def _match_family(token: str, policy: LicensePolicy) -> LicenseFamily | None:
    for family in policy.restrictive:
        if any(pattern.search(token) for pattern in family.patterns):
            return family
    for family in policy.permissive:
        if any(pattern.search(token) for pattern in family.patterns):
            return family
    return None


def classify_license(expression: str | None, policy: LicensePolicy) -> LicenseClassification:
    """Split an expression into license tokens and classify each against the policy.

    ``GPL-2.0-only OR MIT`` is restrictive (one part is enough), ``MIT AND Apache-2.0``
    is permissive, and anything with a token the policy does not know is not covered,
    which the caller reports as insufficient evidence rather than compliant.
    """
    raw = (expression or "").strip()
    # An exception ("Apache-2.0 WITH LLVM-exception") modifies the license before it
    # rather than adding one, so only the base license is classified.
    base = re.sub(r"\s+with\s+[^\s()]+", "", raw, flags=re.IGNORECASE)
    # Operators are whitespace separated ("GPL-2.0-only OR MIT"). Splitting on a bare
    # \bor\b would also split the "-or-later" suffix off a license id.
    tokens = [part.strip() for part in re.split(r"\s+(?:and|or)\s+|[()]", base.lower())]
    tokens = [token for token in tokens if token]

    restrictive: list[LicenseFamily] = []
    unknown = False
    for token in tokens:
        family = _match_family(token, policy)
        if family is None:
            unknown = True
        elif family.restrictive:
            restrictive.append(family)

    if restrictive:
        worst = max(
            restrictive,
            key=lambda family: SEVERITY_ORDER[family.severity or policy.default_severity],
        )
        return LicenseClassification(
            expression=raw,
            restrictive=True,
            covered=not unknown,
            family=worst.name,
            severity=worst.severity or policy.default_severity,
            summary=worst.summary,
        )
    return LicenseClassification(
        expression=raw,
        restrictive=False,
        covered=bool(tokens) and not unknown,
        family=None,
        severity=None,
        summary=None,
    )


# --- registry metadata ------------------------------------------------------


@dataclass(frozen=True)
class DeclaredLicense:
    """The license a registry publishes for one package version."""

    license: str | None
    source: str  # pypi, npm, scancode
    detail: str | None = None  # why there is no license, for the finding


def _fetch_json(url: str, origin: str, timeout: int) -> dict:
    request = urlrequest.Request(
        url,
        headers={
            "User-Agent": "due-diligence-license-scan/1.0",
            "Accept": "application/json",
        },
    )
    with urlrequest.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https origin
        # A redirect must not move the lookup to another host.
        if not response.geturl().startswith(origin + "/"):
            raise ValueError("registry redirected the lookup to an unexpected origin")
        body = response.read(MAX_METADATA_BYTES + 1)
    if len(body) > MAX_METADATA_BYTES:
        raise ValueError("registry response is too large")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("registry response is not a JSON object")
    return payload


def _clean_license(value: object) -> str | None:
    if isinstance(value, dict):
        value = value.get("type")
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text or text.upper() in {"UNKNOWN", "NONE", "NOASSERTION"}:
        return None
    return text[:200]


def lookup_pypi_license(name: str, version: str, timeout: int) -> DeclaredLicense:
    if not PYPI_NAME.match(name) or not VERSION_PATTERN.match(version):
        return DeclaredLicense(None, "pypi", "package name or version is not a plain identifier")
    url = f"{PYPI_ORIGIN}/pypi/{urlparse.quote(name, safe='')}/{urlparse.quote(version, safe='')}/json"
    try:
        payload = _fetch_json(url, PYPI_ORIGIN, timeout)
    except urlerror.HTTPError as exc:
        return DeclaredLicense(None, "pypi", f"PyPI returned HTTP {exc.code}")
    except (urlerror.URLError, TimeoutError, OSError) as exc:
        return DeclaredLicense(None, "pypi", f"PyPI lookup failed: {type(exc).__name__}")
    except ValueError as exc:
        return DeclaredLicense(None, "pypi", f"PyPI lookup failed: {exc}")

    info = payload.get("info") or {}
    for candidate in (info.get("license_expression"), info.get("license")):
        cleaned = _clean_license(candidate)
        if cleaned:
            return DeclaredLicense(cleaned, "pypi", None)
    for classifier in info.get("classifiers") or []:
        if isinstance(classifier, str) and classifier.startswith("License ::"):
            cleaned = _clean_license(classifier.split("::")[-1])
            if cleaned:
                return DeclaredLicense(cleaned, "pypi", None)
    return DeclaredLicense(None, "pypi", "PyPI publishes no license for this version")


def lookup_npm_license(name: str, version: str, timeout: int) -> DeclaredLicense:
    if not NPM_NAME.match(name) or not VERSION_PATTERN.match(version):
        return DeclaredLicense(None, "npm", "package name or version is not a plain identifier")
    url = f"{NPM_ORIGIN}/{urlparse.quote(name, safe='')}/{urlparse.quote(version, safe='')}"
    try:
        payload = _fetch_json(url, NPM_ORIGIN, timeout)
    except urlerror.HTTPError as exc:
        return DeclaredLicense(None, "npm", f"npm returned HTTP {exc.code}")
    except (urlerror.URLError, TimeoutError, OSError) as exc:
        return DeclaredLicense(None, "npm", f"npm lookup failed: {type(exc).__name__}")
    except ValueError as exc:
        return DeclaredLicense(None, "npm", f"npm lookup failed: {exc}")

    for candidate in (payload.get("license"), payload.get("licenses")):
        if isinstance(candidate, list):
            names = [cleaned for item in candidate if (cleaned := _clean_license(item))]
            if names:
                return DeclaredLicense(" OR ".join(names), "npm", None)
            continue
        cleaned = _clean_license(candidate)
        if cleaned:
            return DeclaredLicense(cleaned, "npm", None)
    return DeclaredLicense(None, "npm", "npm publishes no license for this version")


def lookup_declared_license(
    ecosystem: str, name: str, version: str, *, timeout: int = 15
) -> DeclaredLicense:
    """Fetch the declared license for one pinned package version."""
    if ecosystem == "pypi":
        return lookup_pypi_license(name, version, timeout)
    if ecosystem == "npm":
        return lookup_npm_license(name, version, timeout)
    return DeclaredLicense(None, ecosystem or "unknown", "no license source for this ecosystem")


# --- report -----------------------------------------------------------------


@dataclass(frozen=True)
class FileLicense:
    path: str
    expression: str
    spdx: str | None
    line_start: int
    line_end: int
    rule_id: str | None
    matcher: str | None
    score: float | None


@dataclass(frozen=True)
class Dependency:
    ecosystem: str
    name: str
    version: str
    manifest_path: str
    requirement: str
    pinned: bool
    declared: DeclaredLicense | None = None  # set when the report already carries it


@dataclass(frozen=True)
class ParsedReport:
    files: tuple[FileLicense, ...]
    dependencies: tuple[Dependency, ...]
    unreadable_files: tuple[str, ...]
    header_errors: tuple[str, ...]
    version: str | None


@dataclass(frozen=True)
class ScancodeRunResult:
    report: ParsedReport | None
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


def _mount_prefix() -> str:
    return Path(SNAPSHOT_MOUNT).name + "/"


def _relative_path(raw: str) -> str:
    path = str(raw or "").replace("\\", "/").lstrip("/")
    prefix = _mount_prefix()
    if path.startswith(prefix):
        path = path[len(prefix) :]
    return path


def _parse_purl(purl: str) -> tuple[str, str, str] | None:
    """`pkg:npm/%40ckeditor/ckeditor5-core@41.4.2` to (npm, @ckeditor/ckeditor5-core, 41.4.2).

    npm dependencies parsed from a bare package.json carry no version in the purl
    (ScanCode does not resolve them), so the version may come back empty here.
    """
    match = PURL_PATTERN.match(str(purl or ""))
    if match is None:
        return None
    ecosystem = match.group("type").lower()
    name = urlparse.unquote(match.group("name"))
    version = urlparse.unquote(match.group("version") or "")
    if not name:
        return None
    return ecosystem, name, version


def _file_licenses(path: str, payload: dict) -> list[FileLicense]:
    """One record per detected license expression, at its first cited line range."""
    found: list[FileLicense] = []
    for detection in payload.get("license_detections") or []:
        matches = [m for m in detection.get("matches") or [] if isinstance(m, dict)]
        if not matches:
            continue
        start = min(int(m.get("start_line") or 1) for m in matches)
        end = max(int(m.get("end_line") or start) for m in matches)
        first = matches[0]
        found.append(
            FileLicense(
                path=path,
                expression=str(detection.get("license_expression") or ""),
                spdx=detection.get("license_expression_spdx"),
                line_start=max(1, start),
                line_end=max(start, end),
                rule_id=first.get("rule_identifier"),
                matcher=first.get("matcher"),
                score=first.get("score"),
            )
        )
    return found


def _declared_from_package(package: dict) -> DeclaredLicense | None:
    for key in ("declared_license_expression", "declared_license_expression_spdx"):
        cleaned = _clean_license(package.get(key))
        if cleaned:
            return DeclaredLicense(cleaned, "scancode", None)
    statement = package.get("extracted_license_statement")
    cleaned = _clean_license(statement)
    if cleaned:
        return DeclaredLicense(cleaned, "scancode", None)
    return None


def parse_report(payload: dict) -> ParsedReport:
    """Normalize a ScanCode JSON report into licenses, dependencies and gaps."""
    header = (payload.get("headers") or [{}])[0] or {}
    header_errors = tuple(str(item) for item in header.get("errors") or [])

    files: list[FileLicense] = []
    unreadable: list[str] = []
    by_purl: dict[str, DeclaredLicense] = {}

    for entry in payload.get("packages") or []:
        if not isinstance(entry, dict):
            continue
        purl = entry.get("purl") or entry.get("package_uid")
        declared = _declared_from_package(entry)
        if purl and declared:
            by_purl[str(purl).split("?", 1)[0]] = declared

    for entry in payload.get("files") or []:
        if not isinstance(entry, dict):
            continue
        path = _relative_path(entry.get("path") or "")
        if entry.get("scan_errors"):
            unreadable.append(path)
        files.extend(_file_licenses(path, entry))
        for package in entry.get("package_data") or []:
            if not isinstance(package, dict):
                continue
            declared = _declared_from_package(package)
            for dependency in package.get("dependencies") or []:
                purl = str((dependency or {}).get("purl") or "").split("?", 1)[0]
                if purl and declared:
                    by_purl.setdefault(purl, declared)

    raw_dependencies = list(payload.get("dependencies") or [])
    if not raw_dependencies:
        # Older reports keep dependencies only inside each file's package data.
        for entry in payload.get("files") or []:
            for package in (entry or {}).get("package_data") or []:
                raw_dependencies.extend((package or {}).get("dependencies") or [])

    dependencies: list[Dependency] = []
    seen: set[tuple[str, str, str, str]] = set()
    for dependency in raw_dependencies:
        if not isinstance(dependency, dict):
            continue
        parsed = _parse_purl(dependency.get("purl"))
        if parsed is None:
            continue
        ecosystem, name, version = parsed
        requirement = str(dependency.get("extracted_requirement") or "")
        if not version and EXACT_VERSION.match(requirement.strip()):
            # "@ckeditor/ckeditor5-core": "41.4.2" is an exact pin even though the
            # purl has no version: ScanCode does not resolve npm manifests itself.
            version = requirement.strip().lstrip("=").lstrip("v")
        pinned = bool(version)
        manifest = _relative_path(dependency.get("datafile_path") or "")
        key = (ecosystem, name, version, manifest)
        if key in seen:
            continue
        seen.add(key)
        purl = str(dependency.get("purl") or "").split("?", 1)[0]
        dependencies.append(
            Dependency(
                ecosystem=ecosystem,
                name=name,
                version=version,
                manifest_path=manifest,
                requirement=requirement,
                pinned=pinned,
                declared=by_purl.get(purl),
            )
        )

    return ParsedReport(
        files=tuple(files),
        dependencies=tuple(dependencies),
        unreadable_files=tuple(unreadable),
        header_errors=header_errors,
        version=str(header.get("tool_version") or "") or None,
    )


# --- docker -----------------------------------------------------------------


def build_command(
    snapshot: Path, output_dir: Path, config: ScancodeConfig = ScancodeConfig()
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
        "--license",
        "--package",
        "--json-pp",
        f"{OUTPUT_MOUNT}/report.json",
        "--processes",
        str(config.processes),
        "--quiet",
        SNAPSHOT_MOUNT,
    ]


def scanner_version(config: ScancodeConfig = ScancodeConfig()) -> str | None:
    """Read the version from the image label; no container start needed."""
    try:
        completed = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                f'{{{{ index .Config.Labels "{VERSION_LABEL}" }}}}',
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


def run_scancode(
    snapshot: Path, output_dir: Path, *, config: ScancodeConfig = ScancodeConfig()
) -> ScancodeRunResult:
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
        return ScancodeRunResult(
            report=None,
            exit_code=-1,
            error=f"scancode timed out after {config.timeout_seconds}s",
            report_path=None,
        )
    except OSError as exc:
        return ScancodeRunResult(
            report=None,
            exit_code=-1,
            error=redact_text(f"could not start scancode: {exc}")[:MAX_ERROR_CHARS],
            report_path=None,
        )

    stderr = redact_text(completed.stderr or "")[:MAX_ERROR_CHARS]
    if completed.returncode != 0:
        return ScancodeRunResult(
            report=None,
            exit_code=completed.returncode,
            error=f"scancode exited with {completed.returncode}: {stderr}".strip(),
            report_path=None,
        )
    if not report_path.is_file():
        return ScancodeRunResult(
            report=None,
            exit_code=completed.returncode,
            error="scancode produced no report file",
            report_path=None,
        )

    try:
        payload = json.loads(report_path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as exc:
        return ScancodeRunResult(
            report=None,
            exit_code=completed.returncode,
            error=redact_text(f"could not parse scancode report: {exc}")[:MAX_ERROR_CHARS],
            report_path=None,
        )

    report = parse_report(payload)
    if report.header_errors:
        # Without a complete inventory a license conclusion would be a guess.
        return ScancodeRunResult(
            report=None,
            exit_code=completed.returncode,
            error=redact_text(
                "scancode report is incomplete: " + "; ".join(report.header_errors)
            )[:MAX_ERROR_CHARS],
            report_path=report_path,
        )
    return ScancodeRunResult(
        report=report,
        exit_code=completed.returncode,
        error=None,
        report_path=report_path,
    )


# --- helpers ----------------------------------------------------------------


def _read_snippet(root: Path, relative_path: str, line_start: int, line_end: int) -> str:
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


def find_declaration_line(manifest: Path, name: str, requirement: str) -> tuple[int, int, str]:
    """Locate the line that declares a dependency, for the evidence row."""
    try:
        lines = manifest.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 1, 1, ""
    lowered = name.lower()
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//")):
            continue
        if requirement and requirement in stripped:
            return number, number, stripped[:MAX_SNIPPET_CHARS]
        if lowered in stripped.lower():
            return number, number, stripped[:MAX_SNIPPET_CHARS]
    return 1, 1, ""


@dataclass(frozen=True)
class _Candidate:
    """One finding before it is written."""

    rule_id: str
    title: str
    severity: Severity
    verdict: Verdict
    verification: VerificationStatus
    confidence: Decimal | None
    reasoning: str
    recommendation: str
    file_path: str
    line_start: int
    line_end: int
    snippet: str


# --- persistence ------------------------------------------------------------


def _plan(
    report: ParsedReport,
    snapshot: Path,
    policy: LicensePolicy,
    config: ScancodeConfig,
) -> list[_Candidate]:
    """Turn a parsed report into the findings this audit should carry."""
    candidates: list[_Candidate] = []
    lookup_cache: dict[tuple[str, str, str], DeclaredLicense] = {}

    for license_hit in report.files:
        expression = license_hit.spdx or license_hit.expression
        if not expression:
            continue
        classification = classify_license(expression, policy)
        snippet = redact_text(
            _read_snippet(snapshot, license_hit.path, license_hit.line_start, license_hit.line_end)
        )[:MAX_SNIPPET_CHARS]
        if classification.restrictive:
            candidates.append(
                _Candidate(
                    rule_id=f"scancode:{expression}",
                    title=f"Restrictive license {expression} in {license_hit.path}",
                    severity=classification.severity or policy.default_severity,
                    verdict=Verdict.ISSUE,
                    verification=VerificationStatus.SCANNER_CONFIRMED,
                    confidence=Decimal("0.90"),  # scanned license text, not a judgement call
                    reasoning=(
                        f"ScanCode detected {expression} in {license_hit.path}:"
                        f"{license_hit.line_start}-{license_hit.line_end}"
                        f" (rule {license_hit.rule_id or 'unknown'},"
                        f" matcher {license_hit.matcher or 'unknown'},"
                        f" score {license_hit.score if license_hit.score is not None else 'n/a'})."
                        f" The license policy classifies it as restrictive family"
                        f" {classification.family}."
                    ),
                    recommendation=(
                        f"{classification.summary or ''} Confirm the obligations with counsel,"
                        " then replace the component, isolate it, or obtain a commercial license."
                    ).strip(),
                    file_path=license_hit.path,
                    line_start=license_hit.line_start,
                    line_end=license_hit.line_end,
                    snippet=snippet,
                )
            )
        elif not classification.covered:
            candidates.append(
                _Candidate(
                    rule_id="license-policy:not-covered",
                    title=f"License {expression or 'unknown'} in {license_hit.path} is not covered by the policy",
                    severity=Severity.LOW,
                    verdict=Verdict.INSUFFICIENT_EVIDENCE,
                    verification=VerificationStatus.UNRESOLVED,
                    confidence=None,
                    reasoning=(
                        f"ScanCode detected {expression or 'an unnamed license'} in"
                        f" {license_hit.path}:{license_hit.line_start}-{license_hit.line_end},"
                        " but the license policy lists neither it nor a family it belongs to."
                        " Not classified as permissive."
                    ),
                    recommendation=(
                        "Add the license to the policy after review, or ask the supplier for"
                        " clarified terms before relying on this component."
                    ),
                    file_path=license_hit.path,
                    line_start=license_hit.line_start,
                    line_end=license_hit.line_end,
                    snippet=snippet,
                )
            )

    unpinned: dict[str, list[Dependency]] = {}
    for dependency in report.dependencies:
        manifest = snapshot / dependency.manifest_path
        line_start, line_end, snippet = find_declaration_line(
            manifest, dependency.name, dependency.requirement
        )

        if not dependency.pinned:
            unpinned.setdefault(dependency.manifest_path, []).append(dependency)
            continue

        declared = dependency.declared
        if declared is None:
            key = (dependency.ecosystem, dependency.name.lower(), dependency.version)
            if key not in lookup_cache:
                if config.allow_network:
                    lookup_cache[key] = lookup_declared_license(
                        dependency.ecosystem,
                        dependency.name,
                        dependency.version,
                        timeout=config.metadata_timeout_seconds,
                    )
                else:
                    lookup_cache[key] = DeclaredLicense(
                        None, dependency.ecosystem, "registry lookup is disabled (allow_network)"
                    )
            declared = lookup_cache[key]

        label = f"{dependency.name} {dependency.version}"
        location = f"{dependency.manifest_path}:{line_start}"
        if declared.license is None:
            candidates.append(
                _Candidate(
                    rule_id="license-policy:missing",
                    title=f"No license data for {label}",
                    severity=Severity.LOW,
                    verdict=Verdict.INSUFFICIENT_EVIDENCE,
                    verification=VerificationStatus.UNRESOLVED,
                    confidence=None,
                    reasoning=(
                        f"{label} is declared in {location}, but no license could be"
                        f" established: {declared.detail or 'no license data'}."
                        " Missing license data is not evidence of compliance."
                    ),
                    recommendation=(
                        f"Obtain the license for {dependency.name} {dependency.version} from the"
                        " maintainer or registry, then re-run the license scan."
                    ),
                    file_path=dependency.manifest_path,
                    line_start=line_start,
                    line_end=line_end,
                    snippet=snippet,
                )
            )
            continue

        classification = classify_license(declared.license, policy)
        if classification.restrictive:
            candidates.append(
                _Candidate(
                    rule_id=f"license-policy:{classification.family}",
                    title=f"{label} is {declared.license} ({classification.family})",
                    severity=classification.severity or policy.default_severity,
                    verdict=Verdict.ISSUE,
                    verification=VerificationStatus.SCANNER_CONFIRMED,
                    confidence=Decimal("0.80"),  # declared metadata, not scanned license text
                    reasoning=(
                        f"{declared.source} publishes the license for {label} as"
                        f" '{declared.license}'. Declared in {location}."
                        f" The license policy classifies it as restrictive family"
                        f" {classification.family}."
                    ),
                    recommendation=(
                        f"{classification.summary or ''} Confirm the obligations with counsel,"
                        " then replace the dependency, isolate it, or obtain a commercial license."
                    ).strip(),
                    file_path=dependency.manifest_path,
                    line_start=line_start,
                    line_end=line_end,
                    snippet=snippet,
                )
            )
        elif not classification.covered:
            candidates.append(
                _Candidate(
                    rule_id="license-policy:not-covered",
                    title=f"{label} is {declared.license}, which the policy does not cover",
                    severity=Severity.LOW,
                    verdict=Verdict.INSUFFICIENT_EVIDENCE,
                    verification=VerificationStatus.UNRESOLVED,
                    confidence=None,
                    reasoning=(
                        f"{declared.source} publishes the license for {label} as"
                        f" '{declared.license}', declared in {location}, but the license policy"
                        " covers neither it nor a family it belongs to. Not classified as"
                        " permissive."
                    ),
                    recommendation=(
                        "Add the license to the policy after review, or ask the supplier for"
                        " clarified terms before relying on this dependency."
                    ),
                    file_path=dependency.manifest_path,
                    line_start=line_start,
                    line_end=line_end,
                    snippet=snippet,
                )
            )

    for manifest_path, dependencies in unpinned.items():
        first = dependencies[0]
        line_start, line_end, snippet = find_declaration_line(
            snapshot / manifest_path, first.name, first.requirement
        )
        # Unpinned dependencies have no version; their declared range is the evidence.
        listed = ", ".join(
            f"{d.name} {d.requirement or d.version}".strip() for d in dependencies[:5]
        )
        candidates.append(
            _Candidate(
                rule_id="license-policy:unpinned",
                title=(
                    f"{len(dependencies)} dependencies in {manifest_path} are not pinned,"
                    " so their licenses were not verified"
                ),
                severity=Severity.LOW,
                verdict=Verdict.INSUFFICIENT_EVIDENCE,
                verification=VerificationStatus.UNRESOLVED,
                confidence=None,
                reasoning=(
                    f"{manifest_path} declares {len(dependencies)} dependencies without an exact"
                    f" version ({listed}{', ...' if len(dependencies) > 5 else ''}), so the"
                    " installed versions and their licenses are unknown."
                ),
                recommendation=(
                    "Pin these dependencies to exact versions, then re-run the license scan."
                ),
                file_path=manifest_path,
                line_start=line_start,
                line_end=line_end,
                snippet=snippet,
            )
        )

    if report.unreadable_files:
        first = report.unreadable_files[0]
        listed = ", ".join(report.unreadable_files[:5])
        candidates.append(
            _Candidate(
                rule_id="scancode:incomplete",
                title=(
                    f"License inventory is incomplete: {len(report.unreadable_files)}"
                    " file(s) could not be scanned"
                ),
                severity=Severity.LOW,
                verdict=Verdict.INSUFFICIENT_EVIDENCE,
                verification=VerificationStatus.UNRESOLVED,
                confidence=None,
                reasoning=(
                    f"ScanCode reported scan errors for {len(report.unreadable_files)} file(s)"
                    f" ({listed}{', ...' if len(report.unreadable_files) > 5 else ''}), so any"
                    " license they contain is missing from this inventory."
                ),
                recommendation="Review the scanner errors, then re-run the license scan.",
                file_path=first,
                line_start=1,
                line_end=1,
                snippet="",
            )
        )

    return candidates


def scan_audit(
    session: Session,
    audit_id: uuid.UUID,
    snapshot_root: Path,
    *,
    config: ScancodeConfig = ScancodeConfig(),
    output_root: Path | None = None,
) -> ScanSummary:
    """Run ScanCode for one audit and store license findings and evidence."""
    audit = session.get(Audit, audit_id)
    if audit is None:
        raise ValueError(f"audit {audit_id} does not exist")

    run = ScannerRun(
        audit_id=audit_id,
        tool=ScannerTool.SCANCODE,
        version=scanner_version(config),
        status=ScannerRunStatus.RUNNING,
        started_at=datetime.now(timezone.utc),
    )
    session.add(run)
    session.commit()
    session.refresh(run)

    output_dir = (output_root or Path("./data/scanner-output")) / str(audit_id) / "scancode"
    result = run_scancode(snapshot_root, output_dir, config=config)

    if result.error is not None or result.report is None:
        run.status = ScannerRunStatus.FAILED
        run.error = result.error or "scancode produced no usable report"
        run.finished_at = datetime.now(timezone.utc)
        session.commit()
        logger.warning("scancode failed for audit %s: %s", audit_id, run.error)
        return ScanSummary(
            scanner_run_id=run.id,
            status=ScannerRunStatus.FAILED,
            detected=0,
            stored=0,
            error=run.error,
        )

    policy = load_policy(config.policy_path)
    candidates = _plan(result.report, Path(snapshot_root), policy, config)

    existing = existing_fingerprints(session, audit_id)

    stored = 0
    for item in candidates:
        fingerprint = finding_fingerprint(
            category=Category.LICENSE.value,
            rule_id=item.rule_id,
            file_path=item.file_path,
            line_start=item.line_start,
            line_end=item.line_end,
        )
        if fingerprint in existing:
            continue

        finding = Finding(
            audit_id=audit_id,
            category=Category.LICENSE,
            title=item.title[:300],
            severity=item.severity,
            confidence=item.confidence,
            origin=FindingOrigin.SCANNER,
            source_tool=item.rule_id,
            verification_status=item.verification,
            verdict=item.verdict,
            reasoning=item.reasoning,
            business_impact=(
                "A restrictive or unknown license can oblige the product to release source,"
                " restrict redistribution, or block a sale until the terms are clarified."
                if item.verdict == Verdict.ISSUE
                else "An unverified license leaves the product's redistribution rights unknown."
            ),
            recommendation=item.recommendation,
            fingerprint=fingerprint,
        )
        session.add(finding)
        session.flush()  # assign finding.id before the evidence row

        session.add(
            Evidence(
                finding_id=finding.id,
                file_id=None,  # linked to files when indexing has run for this audit
                file_path=item.file_path,
                line_start=item.line_start,
                line_end=item.line_end,
                snippet=item.snippet or None,
                kind=EvidenceKind.PRIMARY,
            )
        )
        existing.add(fingerprint)
        stored += 1

    run.status = ScannerRunStatus.SUCCEEDED
    run.version = result.report.version or run.version
    run.finished_at = datetime.now(timezone.utc)
    run.raw_output_path = str(result.report_path)
    session.commit()

    logger.info(
        "scancode finished for audit %s: %d detected, %d stored",
        audit_id,
        len(candidates),
        stored,
    )
    return ScanSummary(
        scanner_run_id=run.id,
        status=ScannerRunStatus.SUCCEEDED,
        detected=len(candidates),
        stored=stored,
        error=None,
    )
