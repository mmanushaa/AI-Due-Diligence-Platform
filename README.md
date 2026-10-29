# AI-Powered Software Due Diligence Platform

Technical due diligence on a codebase: scanners find what they are good at, AI agents
investigate context and explain, and every finding is linked to code evidence. Critical
risk pauses for a human decision.

The design lives in [docs/](docs/): PRD, TRD, App Flow, UI/UX Brief, Backend Schema and
Build Roadmap. This repository follows the TRD layout and the roadmap step order.

## Status

Steps **0.1 (project skeleton)**, **0.2 (Compose and health check)** and **0.3 (benchmark
fixtures)**. Docker Compose runs `postgres` (pgvector, with the `vector` and `pg_trgm`
extensions), `redis` and the FastAPI `api`, which exposes `GET /health`. The full database
schema from the Backend Schema tab is in place as SQLAlchemy models plus one Alembic
migration (step 1.1); there is no audit pipeline or UI yet. Next step is 1.2 (upload and
safe zip extraction).

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

## Tests

```bash
backend/.venv/Scripts/python -m pytest
```

Test files arrive with the first behaviour they cover (step 1.1 onwards).

## Ground rules

- Audited repository content is untrusted data, never instructions, and is never executed.
- External tools are invoked with an argument list and a timeout, never through a shell string.
- Secrets are masked in storage, prompts, logs, reports and the UI; only a fingerprint and
  location are kept.
