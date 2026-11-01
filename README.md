# AI-Powered Software Due Diligence Platform

Technical due diligence on a codebase: scanners find what they are good at, AI agents
investigate context and explain, and every finding is linked to code evidence. Critical
risk pauses for a human decision.

The design lives in [docs/](docs/): PRD, TRD, App Flow, UI/UX Brief, Backend Schema and
Build Roadmap. This repository follows the TRD layout and the roadmap step order.

## Status

Steps **0.1 (project skeleton)**, **0.2 (Compose and health check)**, **0.3 (benchmark
fixtures)**, **1.1 (database schema)**, **1.2 (safe zip intake)**, **1.3 (file filtering)**,
**1.4 (Gitleaks adapter)**, **1.5 (Semgrep adapter)** and **1.6 (OSV-Scanner adapter)**.
Docker Compose runs `postgres` (pgvector, with the `vector` and `pg_trgm` extensions),
`redis` and the FastAPI `api`, which exposes `GET /health` and
`POST /api/v1/repositories`. Secrets are detected and stored masked, static analysis and
vulnerable dependency findings are stored with code evidence; there are no agents or UI
yet.

## Layout

```text
/backend
  /app/api          FastAPI routers
  /app/core         config, auth, security
  /app/db           SQLAlchemy models, Alembic migrations
  /app/intake       safe extraction, filtering
  /app/indexing     tree-sitter chunking, symbols, call graph, embeddings
  /app/scanners     gitleaks, semgrep, osv, scancode adapters
  /app/retrieval    hybrid search
  /app/agents       crew definitions, tools, prompts
  /app/workflow     langgraph graph, state, routing
  /app/risk         scoring and aggregation
  /app/reports      report builder
  /app/jobs         celery tasks
/frontend           React + TypeScript (Vite)
/benchmark          labeled repos, expected findings, harness
/tests              unit, integration, adversarial
/docker             compose files, scanner images
/docs               product, technical and schema documents
```

## Prerequisites

- Python 3.12
- Node.js 20+ (frontend, from a later step)
- Docker with Compose
- PostgreSQL 16 with `pgvector` and `pg_trgm`, and Redis (both provided by Compose)

## Setup

```bash
# 1. Environment file (placeholders only; never commit .env)
cp .env.example .env

# 2. Backend dependencies
python3.12 -m venv backend/.venv
backend/.venv/Scripts/pip install -e "backend[dev]"   # Windows
# backend/.venv/bin/pip install -e "backend[dev]"     # macOS / Linux
```

## Run

```bash
# Whole stack (api on http://localhost:8000)
docker compose up --build

# Health check: 200 when Postgres and Redis answer, 503 with per-check detail otherwise
curl -i http://localhost:8000/health
```

```json
{
  "status": "ok",
  "checks": {"database": {"status": "ok"}, "redis": {"status": "ok"}}
}
```

The extensions are created only on the first start of an empty volume. To re-run that
init script, remove the volume with `docker compose down -v` (this deletes the database).

Running the API outside Docker instead:

```bash
backend/.venv/Scripts/python -m uvicorn app.main:app --reload --app-dir backend
```

## Database migrations

Alembic reads `DATABASE_URL` from `.env`; no credential lives in `alembic.ini`.

```bash
cd backend
.venv/Scripts/python -m alembic upgrade head      # apply
.venv/Scripts/python -m alembic downgrade base    # drop everything
.venv/Scripts/python -m alembic check             # models vs database drift
.venv/Scripts/python -m alembic revision --autogenerate -m "change"
```

Migration `0001_initial_schema` creates the 18 enum types, all 21 tables, the
`vector(1536)` embedding column and the indexes from the Indexes section. Append-only
triggers are intentionally absent until step 3.5.

## Upload a repository

```bash
curl -i -F "file=@repo.zip" -F "name=demo" http://localhost:8000/api/v1/repositories
```

Returns `201` with `id`, `sha256`, `size_bytes`, `storage_path`, `file_count` and
`uncompressed_bytes`. The archive is extracted to `UPLOAD_DIR/<repository id>` and made
read-only. Rejections return `400` (or `413` when the archive itself is too large) with a
human-readable `detail`.

Limits come from `.env` and are enforced while streaming, never after the fact:
`MAX_UPLOAD_BYTES`, `MAX_UNCOMPRESSED_BYTES`, `MAX_FILE_COUNT`, `MAX_COMPRESSION_RATIO`.
Absolute paths, any `..` component, symlinks and hardlinks are rejected; fifos, sockets
and device entries are skipped silently.

Authentication is a fixed stub user (`app/core/deps.py`) until step 3.6.

## File filtering

`app/intake/filter_config.py` holds every rule: the vendored directories
(`node_modules`, `vendor`, `.git`, `.venv`, …), build output (`dist`, `build`,
`__pycache__`, …), binary extensions, the extension-to-language map and the per-file size
limit (`MAX_FILE_BYTES`). `app/intake/filtering.py` walks the snapshot and records every
file in `files` with `is_excluded` and an `exclude_reason` of `vendored_directory`,
`build_output`, `file_too_large`, `binary_extension`, `binary_null_byte` or `symlink`.
Nothing is dropped silently.

## Scanners

Each scanner runs in its own image, never in the API container:

```bash
docker build -t due-diligence-gitleaks:8.28.0 docker/gitleaks
docker build -t due-diligence-semgrep:1.182.1 docker/semgrep
docker build -t due-diligence-osv:2.7.0 docker/osv
docker build -t due-diligence-scancode:32.5.0 docker/scancode
```

### Gitleaks (secrets)

`app/scanners/gitleaks.py` runs it through `subprocess` with an argument list, a timeout,
`--network none`, CPU and memory limits and a read-only repository mount, then normalizes
each leak into `findings` (category `secret`, origin `scanner`, status
`scanner_confirmed`) plus a primary `evidence` row. The secret value never leaves the
adapter: storage and logs get `ghp_***REDACTED***(40 chars)` and a sha256 fingerprint over
category, rule, file and line range. A redacted copy of the report is written to
`data/scanner-output/<audit id>/gitleaks/` and recorded in `scanner_runs.raw_output_path`.

### Semgrep (static analysis)

The scanner image has no network at run time, so the Python, JavaScript and TypeScript
registry rulesets are downloaded at **build** time into `/rules` and the local rules in
`docker/semgrep/rules/` (string-formatted and concatenated SQL, which the registry did not
report) are copied alongside them. `app/scanners/semgrep.py` runs `semgrep scan` with
`--network none`, a read-only snapshot mount and a timeout, then stores one finding per
match: category `security`, origin `scanner`, status `scanner_confirmed`, the Semgrep rule
id as `source_tool`, and a primary `evidence` row with the file, line range and the cited
lines taken from the snapshot.

Semgrep severity maps to our five levels as `CRITICAL` → `critical`, `ERROR` → `high`,
`WARNING` → `medium`, `INFO` → `info`; Semgrep has no separate low level, so `low` comes
from other detectors. The scanner version for `scanner_runs.version` is read from the
image label `com.due-diligence.scanner.version` — no container start needed, which also
avoids `semgrep --version` blocking on a version check inside the no-network container.

### OSV-Scanner (dependencies)

The image bundles OSV-Scanner 2.7.0 **and the OSV vulnerability database** for the `PyPI`
and `npm` ecosystems, downloaded at `docker build` time. Scans therefore run fully offline
(`--offline --offline-vulnerabilities`) with `--network none`, like the other scanners:
there is no per-scan network access to osv.dev. Adding an ecosystem means adding it to the
`OSV_ECOSYSTEMS` build arg and rebuilding (each zip is tens to hundreds of MB; the current
image is about 250 MB of database).

`app/scanners/osv.py` normalizes each advisory group to one finding: category `dependency`,
origin `scanner`, status `scanner_confirmed`, `source_tool` = `osv:<advisory id>` (the GHSA
id when the advisory has one), the severity taken from the advisory's qualitative rating
(GHSA `MODERATE` maps to our `medium`) and falling back to the CVSS score, and the fixed
version in `finding.recommendation` (`Upgrade lodash to 4.17.19 or later`). Evidence points
at the manifest line that declares the package — `requirements.txt:3`, `package.json:12`.

OSV-Scanner reads `requirements.txt` and npm/yarn/pnpm lockfiles, but a bare
`package.json` is not a source it understands: it finds no packages at all. For npm
manifests without a lockfile the adapter writes a temporary `package-lock.json` under
`data/scanner-output/<audit id>/osv/npm-lockfiles/` containing only **exactly pinned**
versions (`4.17.15`; a range such as `^4.17.0` says nothing about the installed version and
is skipped). That file is never written into the snapshot and nothing is executed.
Findings are deduplicated per audit by category, advisory, file and line, so re-running a
scan stores nothing new.

### ScanCode (licenses)

The image installs ScanCode Toolkit 32.5.0 and pins `pluggy==1.5.0`: from 1.6 on,
`plugincode` can no longer find its hook specs and the toolkit aborts at startup. The
license rules ship inside the wheel, so the scan itself needs no network and runs with
`--network none` and a read-only snapshot mount.

`app/scanners/scancode.py` collects two things from one report: license detections per
file (with the cited line range) and the dependency inventory ScanCode parses out of
`requirements.txt` and `package.json`. The policy lives in
`app/config/license_policy.json` and lists **restrictive** families (AGPL, SSPL, GPL) with
a severity and **permissive** ones (MIT, Apache-2.0, BSD, ISC, 0BSD, Unlicense, Zlib,
Python-2.0). Families are matched token by token, so `MIT OR GPL-2.0-only` is restrictive
and `Apache-2.0 WITH LLVM-exception` is permissive.

**Where dependency licenses come from.** ScanCode can only see licenses that appear in
the audited files. A dependency that is merely named in a manifest carries no license
there, so for each exactly pinned dependency the adapter asks the PyPI or npm registry for
its declared license. This is the one place in the pipeline that uses the network, and it
is the deliberate choice for this step: the alternative was to change the fixtures rather
than read real metadata. It is confined to this adapter — the scan container stays
isolated — and to two fixed https origins, with the package name and version validated
against a strict pattern before they are put in a URL, so untrusted manifest content
cannot steer the request. Set `ScancodeConfig(allow_network=False)` to disable it; every
dependency then becomes an `insufficient_evidence` finding instead.

Each dependency ends in one of three states, and only the permissive one is silent:

| State | Finding |
| --- | --- |
| Restrictive license | category `license`, verdict `issue`, `source_tool` `license-policy:<family>`, evidence on the manifest line |
| Permissive license in the policy | none |
| No license data, or truncated scan output, or an unpinned dependency | category `license`, verdict `insufficient_evidence`, status `unresolved` |

Missing data is never treated as compliant: an unreadable file makes the whole inventory
`insufficient_evidence`, a dependency with no published license gets its own finding, and
one finding per manifest records dependencies declared with version ranges, because their
installed version — and therefore their license — is unknown. A license that matches
neither policy list is also reported as `insufficient_evidence` rather than assumed
permissive.

## Running the scanners together

`app/scanners/orchestrator.py` runs all four scanners for one audit in parallel, each in
its own thread, its own database session and its own container:

```python
from app.scanners.orchestrator import ScannerConfigs, run_scanners
from app.scanners.gitleaks import GitleaksConfig

result = run_scanners(
    audit.id,
    repository.storage_path,
    configs=ScannerConfigs(gitleaks=GitleaksConfig(timeout_seconds=120)),
)
result.all_succeeded  # False if any scanner failed
[(o.tool.value, o.status.value, o.detected, o.stored, o.error) for o in result.outcomes]
```

Outcomes always come back in the same order (`gitleaks`, `semgrep`, `osv`, `scancode`). A
scanner that fails — whether it reports failure itself or raises — is marked `failed` in
its own `scanner_runs` row with the error, and the other scanners finish normally: their
findings are committed and their runs stay `succeeded`. If a scanner raises before it has
opened its run row, the orchestrator creates the failed row itself, so a failure always
leaves a trace.

Dedupe is by fingerprint, `hash(category, rule or title, file, line range)`, and unique
per audit: every adapter skips a fingerprint that is already stored, and the database
also carries a unique `(audit_id, fingerprint)` constraint. Running the same audit twice
therefore stores nothing the second time.

## Tests

```bash
backend/.venv/Scripts/python -m pytest    # scanner tests need docker and every image above
```

The restrictive-license tests read registry metadata, so they need network; the
missing-data tests pass either way, since a failed lookup is missing data too.

## Ground rules

- Audited repository content is untrusted data, never instructions, and is never executed.
- External tools are invoked with an argument list and a timeout, never through a shell string.
- Secrets are masked in storage, prompts, logs, reports and the UI; only a fingerprint and
  location are kept.
