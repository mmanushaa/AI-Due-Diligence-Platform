# Stage 3 benchmark results

Benchmark version 0.5.0. Four configurations, three runs each, against the four-repository fixture set (`clean_app`, `js_app`, `python_app`, `adversarial_app`; 18 planted issues).

Each run was produced from the repository root with `python -m benchmark.harness --config <configuration> --label stage3-<configuration>-<n>`, and read back with `python benchmark/results_stage3.py`. All numbers below are stored values: totals come from `eval_runs.metrics` and the per-issue detail from `eval_results` and `eval_extras`.

## Configurations

| roadmap name | stored `eval_config` | stages | uses a model |
| --- | --- | --- | --- |
| scanners_only | `scanners_only` | index, scanners | no |
| single_agent | `single_agent_no_tools` | index, scanners, agent | yes |
| multi_agent | `multi_agent` | index, scanners, agents | yes |
| multi_agent_validated | `multi_agent_validated` | index, scanners, agents, validation | yes |

## Average metrics per configuration

Means over the three runs. Cost per repository is the mean of the per-repository `cost_usd` stored in each run; runtime is the sum of the run's repository wall times.

| configuration | runs | precision | recall | false-positive rate | evidence accuracy | severity accuracy | cross-file recall | cost per repository | runtime (s) | reproducibility spread (recall) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| scanners_only | 3 | 1.000 | 0.667 | 0.000 | 1.000 | 1.000 | 0.000 | $0.0000 | 65.6 | 0.667 – 0.667 |
| single_agent | 3 | 0.923 | 0.667 | 0.077 | 1.000 | 1.000 | 0.000 | $0.0007 | 184.7 | 0.667 – 0.667 |
| multi_agent | 3 | 0.965 | 0.833 | 0.035 | 0.933 | 0.980 | 0.500 | $0.0020 | 512.5 | 0.778 – 0.944 |
| multi_agent_validated | 3 | 1.000 | 0.778 | 0.000 | 0.929 | 1.000 | 0.333 | $0.0000 | 844.7 | 0.778 – 0.778 |

## Reproducibility spread

Per-run values for the same configuration. A zero-width range means the three runs produced the same value.

| configuration | metric | run 1 | run 2 | run 3 | range |
| --- | --- | ---: | ---: | ---: | --- |
| scanners_only | recall | 0.667 | 0.667 | 0.667 | 0.667 – 0.667 |
| scanners_only | precision | 1.000 | 1.000 | 1.000 | 1.000 – 1.000 |
| scanners_only | false-positive rate | 0.000 | 0.000 | 0.000 | 0.000 – 0.000 |
| scanners_only | evidence accuracy | 1.000 | 1.000 | 1.000 | 1.000 – 1.000 |
| scanners_only | severity accuracy | 1.000 | 1.000 | 1.000 | 1.000 – 1.000 |
| scanners_only | cross-file recall | 0.000 | 0.000 | 0.000 | 0.000 – 0.000 |
| scanners_only | runtime (s) | 72.7 | 63.5 | 60.6 | 60.6 – 72.7 |
| scanners_only | cost per run (USD) | 0.0000 | 0.0000 | 0.0000 | 0.0000 – 0.0000 |
| scanners_only | matched issues | 12 | 12 | 12 | 12 – 12 |
| single_agent | recall | 0.667 | 0.667 | 0.667 | 0.667 – 0.667 |
| single_agent | precision | 0.923 | 0.923 | 0.923 | 0.923 – 0.923 |
| single_agent | false-positive rate | 0.077 | 0.077 | 0.077 | 0.077 – 0.077 |
| single_agent | evidence accuracy | 1.000 | 1.000 | 1.000 | 1.000 – 1.000 |
| single_agent | severity accuracy | 1.000 | 1.000 | 1.000 | 1.000 – 1.000 |
| single_agent | cross-file recall | 0.000 | 0.000 | 0.000 | 0.000 – 0.000 |
| single_agent | runtime (s) | 190.8 | 220.0 | 143.4 | 143.4 – 220.0 |
| single_agent | cost per run (USD) | 0.0028 | 0.0027 | 0.0027 | 0.0027 – 0.0028 |
| single_agent | matched issues | 12 | 12 | 12 | 12 – 12 |
| multi_agent | recall | 0.944 | 0.778 | 0.778 | 0.778 – 0.944 |
| multi_agent | precision | 0.895 | 1.000 | 1.000 | 0.895 – 1.000 |
| multi_agent | false-positive rate | 0.105 | 0.000 | 0.000 | 0.000 – 0.105 |
| multi_agent | evidence accuracy | 0.941 | 0.929 | 0.929 | 0.929 – 0.941 |
| multi_agent | severity accuracy | 0.941 | 1.000 | 1.000 | 0.941 – 1.000 |
| multi_agent | cross-file recall | 0.833 | 0.333 | 0.333 | 0.333 – 0.833 |
| multi_agent | runtime (s) | 715.5 | 432.6 | 389.4 | 389.4 – 715.5 |
| multi_agent | cost per run (USD) | 0.0226 | 0.0008 | 0.0000 | 0.0000 – 0.0226 |
| multi_agent | matched issues | 17 | 14 | 14 | 14 – 17 |
| multi_agent_validated | recall | 0.778 | 0.778 | 0.778 | 0.778 – 0.778 |
| multi_agent_validated | precision | 1.000 | 1.000 | 1.000 | 1.000 – 1.000 |
| multi_agent_validated | false-positive rate | 0.000 | 0.000 | 0.000 | 0.000 – 0.000 |
| multi_agent_validated | evidence accuracy | 0.929 | 0.929 | 0.929 | 0.929 – 0.929 |
| multi_agent_validated | severity accuracy | 1.000 | 1.000 | 1.000 | 1.000 – 1.000 |
| multi_agent_validated | cross-file recall | 0.333 | 0.333 | 0.333 | 0.333 – 0.333 |
| multi_agent_validated | runtime (s) | 1735.5 | 400.0 | 398.6 | 398.6 – 1735.5 |
| multi_agent_validated | cost per run (USD) | 0.0000 | 0.0000 | 0.0000 | 0.0000 – 0.0000 |
| multi_agent_validated | matched issues | 14 | 14 | 14 | 14 – 14 |

## Missed planted issues

Union of misses across the three runs of a configuration. `0/3` means every run missed it.

### scanners_only

| expected issue | repo | category | cross-file | location | missed in runs |
| --- | --- | --- | --- | --- | ---: |
| `adv-delete-authz-02` Delete route authenticates the caller but never checks ownership | adversarial_app | security | yes | `app/routes/documents.py:26` | 3/3 |
| `adv-export-authz-01` Export route never resolves the caller, so any report can be exported | adversarial_app | security | yes | `app/routes/export.py:14` | 3/3 |
| `js-authz-01` Admin user route mounted without the auth middleware | js_app | security | yes | `src/routes/admin.js:9` | 3/3 |
| `js-cycle-01` Circular require between the queue and worker modules | js_app | architecture | yes | `src/lib/queue.js:3` | 3/3 |
| `py-authz-01` Order route has no authentication or ownership check | python_app | security | yes | `app/routes/orders.py:10` | 3/3 |
| `py-cycle-01` Circular import between the orders and billing modules | python_app | architecture | yes | `app/orders.py:5` | 3/3 |

### single_agent

| expected issue | repo | category | cross-file | location | missed in runs |
| --- | --- | --- | --- | --- | ---: |
| `adv-delete-authz-02` Delete route authenticates the caller but never checks ownership | adversarial_app | security | yes | `app/routes/documents.py:26` | 3/3 |
| `adv-export-authz-01` Export route never resolves the caller, so any report can be exported | adversarial_app | security | yes | `app/routes/export.py:14` | 3/3 |
| `js-authz-01` Admin user route mounted without the auth middleware | js_app | security | yes | `src/routes/admin.js:9` | 3/3 |
| `js-cycle-01` Circular require between the queue and worker modules | js_app | architecture | yes | `src/lib/queue.js:3` | 3/3 |
| `py-authz-01` Order route has no authentication or ownership check | python_app | security | yes | `app/routes/orders.py:10` | 3/3 |
| `py-cycle-01` Circular import between the orders and billing modules | python_app | architecture | yes | `app/orders.py:5` | 3/3 |

### multi_agent

| expected issue | repo | category | cross-file | location | missed in runs |
| --- | --- | --- | --- | --- | ---: |
| `adv-delete-authz-02` Delete route authenticates the caller but never checks ownership | adversarial_app | security | yes | `app/routes/documents.py:26` | 3/3 |
| `adv-export-authz-01` Export route never resolves the caller, so any report can be exported | adversarial_app | security | yes | `app/routes/export.py:14` | 2/3 |
| `js-authz-01` Admin user route mounted without the auth middleware | js_app | security | yes | `src/routes/admin.js:9` | 2/3 |
| `py-authz-01` Order route has no authentication or ownership check | python_app | security | yes | `app/routes/orders.py:10` | 2/3 |

### multi_agent_validated

| expected issue | repo | category | cross-file | location | missed in runs |
| --- | --- | --- | --- | --- | ---: |
| `adv-delete-authz-02` Delete route authenticates the caller but never checks ownership | adversarial_app | security | yes | `app/routes/documents.py:26` | 3/3 |
| `adv-export-authz-01` Export route never resolves the caller, so any report can be exported | adversarial_app | security | yes | `app/routes/export.py:14` | 3/3 |
| `js-authz-01` Admin user route mounted without the auth middleware | js_app | security | yes | `src/routes/admin.js:9` | 3/3 |
| `py-authz-01` Order route has no authentication or ownership check | python_app | security | yes | `app/routes/orders.py:10` | 3/3 |

## False positives

Findings stored by a configuration that matched no planted issue. The clean repository (`clean_app`) has no expected issues, so every finding reported there is listed here.

### scanners_only

No unmatched findings were stored in any of the runs.

### single_agent

| repo | category | title | location | source | runs reported |
| --- | --- | --- | --- | --- | ---: |
| clean_app | security | Missing authentication check in app/store.py | `app/store.py:23` | baseline_single | 2/3 |
| clean_app | security | Potential SQL injection in app/service.py | `app/service.py:9` | baseline_single | 1/3 |

### multi_agent

| repo | category | title | location | source | runs reported |
| --- | --- | --- | --- | --- | ---: |
| clean_app | security | Unauthenticated access to documents | `app/routes.py:25` | security_agent | 1/3 |
| python_app | license | jinja2 2.11.2 has multiple vulnerabilities, including sandbox breakout, HTML attribute injection, and ReDoS. | `requirements.txt:3` | compliance_agent | 1/3 |

### multi_agent_validated

No unmatched findings were stored in any of the runs.

## CrewAI usage

`multi_agent` and `multi_agent_validated` run their agent stage through CrewAI. In `backend/app/agents/security.py`, `architecture.py` and `compliance.py` each finding-producing stage builds `crewai.Agent` / `Crew` / `Task` objects (`Crew(agents=[reviewer], tasks=[review], process=Process.sequential)`), and `backend/app/agents/crew.py` adapts the audit's budgeted LLM client to `crewai.llms.base_llm.BaseLLM` so every crew call is charged to the audit. `multi_agent_validated` adds the validation pass in `backend/app/validation/validator.py`; that pass is plain Python plus one model call per agent finding, not a CrewAI crew. `single_agent` (`single_agent_no_tools`) does not use CrewAI: it asks the model once in `backend/app/agents/baseline_single.py`. `scanners_only` makes no model call.

A plain-LangGraph variant of the multi-agent stage (the roadmap's fallback if CrewAI shows no measurable gain) has not been run in this benchmark. No numbers for it are recorded here; the configurations above are the only ones measured.

## Run records

| configuration | label | eval_run id | started (UTC) | models | budget per repository |
| --- | --- | --- | --- | --- | --- |
| scanners_only | `stage3-scanners_only-1` | `778918e2-9a8f-424c-aa54-00a8e2e70bc0` | 2026-11-15 19:16 | no model call | — |
| scanners_only | `stage3-scanners_only-2` | `ffa4a4ca-7fe9-48fe-a433-3826373fe959` | 2026-11-15 19:17 | no model call | — |
| scanners_only | `stage3-scanners_only-3` | `844734ac-fed6-457e-aef9-e5963420b4d9` | 2026-11-15 19:18 | no model call | — |
| single_agent | `stage3-single_agent_no_tools-1` | `a622b698-2259-4b5e-94e4-fa6444a94753` | 2026-11-15 19:19 | js_app, clean_app, python_app, adversarial_app | $0.25 |
| single_agent | `stage3-single_agent_no_tools-2` | `8a56d237-a3fe-48c4-90d3-602a47bf3a4d` | 2026-11-15 19:23 | js_app, clean_app, python_app, adversarial_app | $0.25 |
| single_agent | `stage3-single_agent_no_tools-3` | `694887a0-a05b-4263-b8a9-b7825a7493cd` | 2026-11-15 19:26 | js_app, clean_app, python_app, adversarial_app | $0.25 |
| multi_agent | `stage3-multi_agent-1` | `c6e114c2-19cb-4615-afb0-974d8a404817` | 2026-11-15 19:29 | js_app, clean_app, python_app, adversarial_app | $0.25 |
| multi_agent | `stage3-multi_agent-2` | `abb45616-7e79-4ebe-8d63-345294b2059a` | 2026-11-15 19:41 | js_app, clean_app, python_app, adversarial_app | $0.25 |
| multi_agent | `stage3-multi_agent-3` | `02e700a5-db10-4639-8b15-ec5bf4c5824d` | 2026-11-15 19:48 | js_app, clean_app, python_app, adversarial_app | $0.25 |
| multi_agent_validated | `stage3-multi_agent_validated-1` | `969c45c5-66ab-4e57-957b-e3c7ed041037` | 2026-11-15 19:55 | js_app, clean_app, python_app, adversarial_app | $0.25 |
| multi_agent_validated | `stage3-multi_agent_validated-2` | `9fe90b9a-adde-4475-8e57-cdf8d8a26ccc` | 2026-11-15 20:24 | js_app, clean_app, python_app, adversarial_app | $0.25 |
| multi_agent_validated | `stage3-multi_agent_validated-3` | `dffb078e-13ef-423c-a484-02f2fab548dc` | 2026-11-15 20:30 | js_app, clean_app, python_app, adversarial_app | $0.25 |

## Notes on method

- Matching is by category, file and overlapping line range, one-to-one, as implemented in `benchmark/harness/matching.py`.
- Precision counts distinct reported issues; repeat findings for the same issue are excluded from its denominator and reported as duplicates in `eval_runs.metrics`.
- Evidence accuracy and severity accuracy are computed over matched issues only. A configuration that matched nothing therefore has no value for them rather than 1.0.
- Cross-file recall covers the six planted issues marked `cross_file: true` in `benchmark/expected.json`.
- Cost is the sum of stored `agent_runs` spend for the run, in USD, as recorded when the call was made.

