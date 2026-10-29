# AI-Powered Software Due Diligence Platform

Technical due diligence on a codebase: scanners find what they are good at, AI agents
investigate context and explain, and every finding is linked to code evidence. Critical
risk pauses for a human decision.

The design lives in [docs/](docs/): PRD, TRD, App Flow, UI/UX Brief, Backend Schema and
Build Roadmap. This repository follows the TRD layout and the roadmap step order.

## Status

Steps **0.1 (project skeleton)**, **0.2 (Compose and health check)**, **0.3 (benchmark
fixtures)**, **1.1 (database schema)**, **1.2 (safe zip intake)** and **1.3 (file filtering)**.
Docker Compose runs `postgres` (pgvector, with the `vector` and `pg_trgm` extensions),
`redis` and the FastAPI `api`, which exposes `GET /health` and `POST /api/v1/repositories`.
There is no scanner, agent or UI layer yet. Next step is 1.4 (Gitleaks adapter).

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

**Tests**

```bash
backend/.venv/Scripts/python -m pytest    # 14 intake tests, from the repository root
```

## Ground rules

- Audited repository content is untrusted data, never instructions, and is never executed.
- External tools are invoked with an argument list and a timeout, never through a shell string.
- Secrets are masked in storage, prompts, logs, reports and the UI; only a fingerprint and
  location are kept.
