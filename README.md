# AI-Powered Software Due Diligence Platform

Technical due diligence on a codebase: scanners find what they are good at, AI agents
investigate context and explain, and every finding is linked to code evidence. Critical
risk pauses for a human decision.

The design lives in [docs/](docs/): PRD, TRD, App Flow, UI/UX Brief, Backend Schema and
Build Roadmap. This repository follows the TRD layout and the roadmap step order.

## Status

Step **0.1 (project skeleton)** only. There is no application logic, API, database
schema, UI or Docker Compose yet, so nothing is runnable beyond installing
dependencies. Next step in the roadmap is 0.2 (Docker Compose with `postgres`, `redis`,
`api` and `GET /health`).

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
- Docker (from step 0.2)
- PostgreSQL 16 with `pgvector` and `pg_trgm`, and Redis (from step 0.2)

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
# API (no routes exist yet; added in step 0.2)
backend/.venv/Scripts/python -m uvicorn app.main:app --reload --app-dir backend
```

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
