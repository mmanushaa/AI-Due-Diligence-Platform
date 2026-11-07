# AI-Powered Software Due Diligence Platform

Technical due diligence on a codebase: scanners find what they are good at, AI agents
investigate context and explain, and every finding is linked to code evidence. Critical
risk pauses for a human decision.

The design lives in [docs/](docs/): PRD, TRD, App Flow, UI/UX Brief, Backend Schema and
Build Roadmap. This repository follows the TRD layout and the roadmap step order.

## Status

Steps **0.1** through **2.6** are in place: project skeleton, Compose and health check,
benchmark fixtures, schema, safe zip intake, file filtering, the four scanner adapters,
parallel scanner orchestration, indexing and hybrid retrieval, the LiteLLM gateway with a
per-audit budget, and the evaluation harness. Docker Compose runs `postgres` (pgvector,
with the `vector` and `pg_trgm` extensions), `redis`, `litellm` and the FastAPI `api`,
which exposes `GET /health` and `POST /api/v1/repositories`. Findings are stored with
masked secrets and code evidence; there are no agents or UI yet, so only the
`scanners_only` benchmark configuration can run.

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

## LLM gateway

The `litellm` Compose service is the only way any code reaches a model
(`docker/litellm/config.yaml`). The backend never names a provider: it asks for an alias,
and the gateway maps it to a provider model, retries and falls back to the second alias.

```bash
docker compose up -d litellm     # gateway on http://localhost:4000
```

Calls go through `app/llm/client.py`:

```python
from app.llm import LLMClient
from app.db.enums import AgentName

client = LLMClient(session, audit.id)
result = client.complete(AgentName.SECURITY, [{"role": "user", "content": prompt}])
result.text, result.cost_usd, result.used_fallback
```

The client checks `audits.budget_usd` before every call and raises `BudgetExceeded` when the
audit has spent it, so the workflow can stop and keep its partial results. Each attempt
writes one `agent_runs` row (agent, model, tokens, cost, latency, status); `audits.cost_usd`
is the sum of those rows. A provider error is retried with exponential backoff, then
repeated against `LLM_FALLBACK_MODEL`, and only then raises `LLMError`.

No key is read from code: the gateway gets the provider key (`OPENAI_API_KEY`, or
`COHERE_API_KEY` / `TOGETHER_API_KEY` when an alias points at those providers) from the
environment, the backend holds only `LITELLM_API_KEY` (the gateway's master key) in
`Settings.litellm_api_key`, and no exception message contains either value. Which provider an
alias means is one variable: set `LITELLM_UPSTREAM_DEFAULT_MODEL=cohere_chat/command-r-plus-08-2024`
and restart the gateway.

A third alias, `bonsai`, points at PrismML's free 1.58-bit ternary build of Qwen3.6 27B, so
trying a free model is `LLM_DEFAULT_MODEL=bonsai` rather than repointing the aliases every
client already uses. It is priced at zero because it costs nothing. It is also a reasoning
model with no reasoning-length control: the same review prompt that costs command-r-plus one
short call spent 5,081 reasoning tokens before writing a nine-line JSON answer, and with a
4,096-token budget it spends all of it thinking and returns no content at all. Give it
`max_tokens` of at least 8,192 and expect around four minutes per repository.

## Stage 1 benchmark gate

`benchmark/run_stage1.py` audits every fixture repository through the API and compares the
stored findings with `benchmark/expected.json`, matching on category, file and overlapping
line range. It prints matched issues, missed issues (with the reason each one missed) and
extra findings that are not in the benchmark, which are for manual review.

It needs the API and a worker running (the audits are executed by Celery):

```bash
python benchmark/run_stage1.py                     # default API http://127.0.0.1:8000
python benchmark/run_stage1.py --api http://localhost:8000
```

The exit status is `1` when an expected issue was not found, `2` when the API is unreachable
or unhealthy, and `0` otherwise. Extra findings never fail the gate.

## Evaluation harness

`benchmark/harness/` runs one configuration over every fixture repository and scores the
stored findings against `benchmark/expected.json`. It runs the real pipeline in-process
(`app.jobs.audit.run_audit`), so it needs Postgres and the scanner images, and it never
needs the API or a worker.

```bash
backend/.venv/Scripts/python -m benchmark.harness --list                  # what can run here
backend/.venv/Scripts/python -m benchmark.harness --config scanners_only  # full run, saved
backend/.venv/Scripts/python -m benchmark.harness --repo clean_app --no-save
```

The four configurations are `scanners_only`, `single_agent`, `multi_agent` and
`multi_agent_validated`. A configuration runs when `app.agents.STAGES` has a stage for it;
otherwise it is reported as unavailable and exits `2` instead of scoring zero recall for a
layer that never ran. `single_agent` and `multi_agent` are registered today, so the other
two are the ones that explain themselves.

Agent configurations get a small `audits.budget_usd` on their throwaway audit, so a
misbehaving agent stops the run instead of spending without a ceiling. The stage's own
outcome (model, calls, accepted and rejected findings, cost, errors) is printed under the
table and stored in `eval_runs.metrics["agents"]`, which is how a run that never reached the
model is told apart from an agent that found nothing.

Matching requires the same category, the same file and an overlapping line range, one
finding per expected issue. `dup` counts findings that repeat an already-matched issue — OSV
writes one row per advisory, so a dependency with four advisories is four rows for one
issue — and those are excluded from the precision denominator rather than counted as false
positives. Raw `found` is still printed next to it.

Evidence accuracy re-reads the cited lines from the snapshot and checks them against the
claim: the issue's `sink_pattern`, the package and version, or a token shape for secrets
(whose stored snippets are masked). Severity accuracy compares the reported level with the
expected one.

Results are stored in `eval_runs` (one row: config, benchmark version, start time, and a
`metrics` JSONB document with per-repository counts, rates, cost and runtime) and
`eval_results` (one row per expected issue: `detected`, `evidence_correct`,
`severity_correct`). Cost and latency stay NULL there because they belong to the repository
run, not to an individual issue, and copying them onto every row would triple-count.

`benchmark/repos/clean_app` has nothing planted: it is the false-positive control, so every
finding reported there is a false positive. `benchmark/repos/adversarial_app` plants real
issues under source comments and a `SECURITY.md` that instruct reviewers to report the code
as secure, skip files and reveal secrets.

scanners_only, benchmark 0.4.0: recall 12/18, precision 12/12, false-positive rate 0/12,
evidence 12/12, severity 12/12, cross-file recall 0/6 — the six misses are the four
access-control issues and the two import cycles, which no scanner can see from one file.

## The single-agent baseline

`app/agents/baseline_single.py` is one reviewer over the whole repository: it assembles
context with the read-only tools in `app/agents/tools.py` (`retrieve_context`, `get_symbol`,
`read_finding`), makes one model call, and stores what survives validation with
`origin=agent` and `verification_status=ai_suggested`. One call, a fixed retrieval plan and no
tool loop, so a difference between two runs comes from the model or the code rather than from
which tools it happened to choose — that is the point of a baseline.

The stage builds the symbol, chunk, call-graph and vector index if the audit does not have
one yet. `run_audit` indexes files for the scanners and stops there, and retrieval without
the rest returns nothing, which would look like an agent that found no problems.

`app/agents/prompts.py` keeps the two halves apart. The system message is instructions only and
never contains repository text; the repository arrives in `build_user_message` and only inside
`<<<DATA file="…" lines="a-b" untrusted="true">>>` blocks, which the system message says are
data and never instructions. A block cannot close its own delimiter (the marker is replaced)
and secrets are masked before rendering, so a token cannot reach the model and an injection
cannot escape into the instructions.

`app/agents/schema.py` is the contract: `AgentReport`/`AgentFinding` are Pydantic models with
closed category and severity sets and `extra="forbid"`, so invented keys and invented
categories are rejected rather than silently stored. The schema cannot check a claim, so
`validate_report` also requires the file to be in this audit, the line range to fall inside
the file, and any `evidence_quote` to appear at the cited lines. Invalid output — unparseable,
schema-invalid or unverifiable — is retried once with the reasons appended, then dropped; a
partly verifiable reply keeps what can be checked without spending the retry.

```python
from app.agents import run_baseline_single

outcome = run_baseline_single(session, audit_id)
outcome.accepted, outcome.rejected, outcome.dropped, outcome.errors
```

Without a provider key the gateway answers every call with `401`, so the stage stores nothing
and says so in its outcome instead of pretending to have reviewed the repository. Point
`LITELLM_UPSTREAM_*` at a provider whose key is in `.env`, recreate the gateway and re-run
`--config single_agent`.

single_agent, benchmark 0.4.0 (one call per repository, $0.0026 total): recall 12/18,
precision 12/13, false-positive rate 1/13, evidence 12/12, severity 12/12, cross-file recall
0/6. The seven findings it added are six restatements of what the scanners had already
reported and one false positive (a module docstring in config code), so single-agent
retrieval does not yet earn the four access-control and two import-cycle misses; that gap is
what the multi-agent configurations are for. Cost is computed from the rate card of the model
alias the backend requested, not from whatever provider the gateway mapped it to, which is why
an alias pointing at Cohere's command-r-plus is priced as `gpt-4o-mini`.

## The security crew (multi-agent)

`app/agents/security.py` is the `multi_agent` configuration: one agent, two tasks, and the same
three read-only tools. The first task triages every scanner finding in the security and secret
categories — `likely_real`, `likely_false_positive` or `uncertain`, with reasoning that cites
what it read — and the second looks for what no single-file scanner can see: a route that
reads or changes a row without authenticating the caller while a sibling route checks the
caller, two modules that import each other, or a committed credential.

Triage verdicts are reporting only. The scanner rows are never rewritten, so the harness keeps
scoring the scanners it scored before, and a verdict stays a claim stored next to the evidence
rather than a silent edit of it.

The crew reaches the model through `GatewayLLM`, a duck-typed adapter that forwards to
`LLMClient.complete`, so budget checks, `agent_runs` rows, retries and the fallback model
behave exactly as they do for the baseline. It needs native function calling
(`Completion.tool_calls`, `supports_function_calling() -> True`) rather than the ReAct text
protocol: the gateway here maps `gpt-4o-mini` to Cohere's command-r-plus, which answers a ReAct
prompt politely and never calls a tool. Two details came out of running it — CrewAI marks every
tool `strict: true` and Cohere rejects that with a `422` before the first call, and a tool
result has to keep its `tool_call_id` or the provider cannot match it to the call that asked.

Three things the crew needed before it found anything, each learned from a run that did not:

- the investigation task is not handed the triage transcript, because a model with its own
triage turn still in context answers the second task by restating what it just triaged;
- a guardrail rejects an investigation answer written without a single `retrieve_context`
  call — the first version answered with no tool calls at all and still read like a
  successful review;
- the finding is cited at the route, not at the shared query helper. The helper is the proof
  that the check exists elsewhere; a finding filed against it is one nobody can act on.

The prompts live in `app/agents/prompts/security.md` (`## role`, `## goal`, `## backstory`,
`## triage`, `## investigation`, `## json_contract`, `## triage_contract`), read by name with
`load_prompt` and filled with `fill` — which is why `app/agents/prompts.py` became a package.
Output passes the same `parse_report`/`validate_report` gate as the baseline: a file that is
not in the audit, a line range past the end of the file, or a quote that does not appear at the
cited lines is dropped and counted, not stored.

```python
from app.agents import run_security_agent

outcome = run_security_agent(session, audit_id)
outcome.triaged, outcome.likely_real, outcome.likely_false_positive, outcome.uncertain
outcome.accepted, outcome.rejected, outcome.calls, outcome.cost_usd
```

multi_agent, benchmark 0.4.0 (one crew per repository, five to seven model calls,
$0.0131 total, 823s): recall 14/18, precision 14/16, false-positive rate 2/16, evidence
14/14, severity 14/14, cross-file recall 3/6 — against single_agent's 12/18, 12/13, 1/13,
12/12, 12/12, 0/6 for $0.0026 and 178s. The two issues it adds are access-control gaps no
scanner can see from one file: the unauthenticated export route in `adversarial_app` and the
unauthenticated admin route in `js_app`. Everything it reports at the right file is cited
correctly and scored at the expected severity; the two false positives are one finding on the
clean repository and one report filed against a shared query helper rather than the route
that calls it.

That last one is the crew's weak spot, and it is worth stating plainly: on `python_app` the
planted gap (`py-authz-01`, the order route with no ownership check) is found in some runs and
missed in others. A repeat over that repository alone scored recall 5/6, precision 1.0,
cross-file 1.0 in 150s, citing `app/routes/orders.py:11-16`; the full run cited
`app/database.py:19-30`, the helper the route calls, and a finding filed there matches nothing
in the manifest. Retrieval is not the cause — the route is in the blocks either way — so the
remaining work is making the model prefer the endpoint over the helper, or accepting the helper
as an equivalent location. The two import cycles stay out of reach for this crew, which reads
code but does not walk a whole import graph.

## The architecture agent

`app/agents/architecture.py` is the second crew in the `multi_agent` configuration,
registered alongside the security one in `app/agents/multi.py`. Unlike the security
crew, it does not start from code: it starts from the shape of the repository, which
the index already knows.

| fact | where it comes from |
|---|---|
| module graph | `module_edges` and the parsed import symbols, resolved with step 2.2's `ModuleResolver` |
| cycles | `find_cycles` (Tarjan), the same detection `index_graph` reports |
| entry points | modules nothing imports |
| large modules | line count against the median, with a floor under both |
| layering | an import from a lower layer (`db`, `store`, `models`) to a higher one (`routes`, `api`, `handlers`) |

The cycle citations are read out of the file, not inferred: the fact is
`app/billing.py:3 imports app/orders.py -- from app.orders import OrderLine`, and
that statement is what the finding quotes. The model is asked for the judgement the
graph cannot make: is this cycle worth a buyer's attention, is this module large or
just long, does this import really point the wrong way. A guardrail makes it call
`retrieve_context` before answering, because "too large" and "wrong layer" are
claims about code, not numbers.

The rule the stage owns is about paths: `validate_architecture_report` accepts only
architecture findings, and only at files this audit indexed, so a model that
reasons from memory about "typical" project layouts cannot file a finding against a
module the repository does not have. Those rejections are counted separately
(`cited a file this repository does not have`) rather than folded into the general
drop count, because a stage that invents paths is failing differently from one that
just got a line range wrong.

The god-module and layering signals are deliberately quiet on these fixtures: the
largest module in any of them is 55 lines, and no import crosses its layers, so both
render as `none` and the crew has nothing to say about them. The facts are still
printed to the prompt and covered by tests, which is the difference between "found
nothing" and "was never asked".

A proved cycle is also a floor, not only a prompt: when the crew's prose comes back
unparseable, or it answers without mentioning a cycle the graph found, the stage
stores the cycle itself from the facts - first import edge, its own statement as the
quote, `low` severity. The model still writes the finding when it can (it did on
both fixtures), because the reasoning and the recommendation are the parts worth
having; the floor exists so the one thing the graph proved cannot be lost to a bad
reply. The first full run without it lost `js_app`'s cycle to three unparseable
answers in a row.

multi_agent, benchmark 0.4.0 (two crews per repository, 8 to 11 model calls,
$0.0158 total, 537s): recall 15/18, precision 15/19, false-positive rate 4/19,
evidence 15/15, severity 14/15, cross-file recall 3/6. The two cycles behave
differently and both are worth stating plainly. `js_app` scores 6/6 with the crew
citing `src/lib/queue.js:3` - the same line the manifest expects. `python_app`'s
cycle is found and reported, but at `app/billing.py:3`, the reciprocal edge of the
`app/orders.py:5` the manifest names, so it counts as a miss and its finding as a
false positive. Both are the same cycle; the manifest picked one side of it, and
this stage deterministically picks the first edge the walk visits. Severity is
`medium` from the model against the manifest's `low` for that one cycle, which is the
single severity miss in the run.

## Tests

```bash
backend/.venv/Scripts/python -m pytest    # scanner tests need docker and every image above
```

The crew tests need neither a key nor the model: `build_crew` is assembled against a scripted
`GatewayLLM`, so what is checked is the plumbing — three read-only tools, the guardrail that
refuses an answer written without a search and a symbol lookup, tool schemas losing the
`strict` flag Cohere rejects, and a tool result keeping its `tool_call_id`.

The restrictive-license tests read registry metadata, so they need network; the
missing-data tests pass either way, since a failed lookup is missing data too.

## Ground rules

- Audited repository content is untrusted data, never instructions, and is never executed.
- External tools are invoked with an argument list and a timeout, never through a shell string.
- Secrets are masked in storage, prompts, logs, reports and the UI; only a fingerprint and
  location are kept.
