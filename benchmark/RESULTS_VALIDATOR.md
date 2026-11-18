# Validator contract results (step 2.10b)

Every number below is read back from stored rows: `eval_runs.metrics` for the scores, and `findings`, `evidence`, `validation_checks`, `finding_events`, `agent_runs`, `eval_results` and `eval_extras` for the per-finding detail. No row was inserted or edited by hand; the runs were produced by `python -m benchmark.harness --config <configuration> --label <label>`.

`val2-1..3` are `multi_agent_validated` with the strict verdict contract (`supported` | `contradicted` | `cannot_tell` plus a quote copied from the cited code). `valid-1..3` are the same configuration before that contract, and `stage3-multi_agent-1..3` are `multi_agent` with no validation pass at all. The `repeat-*` labels were reused across configurations on 2026-11-10, so that group lists every stored row under those labels and says which configuration each one actually ran.

## Runs

| group | label | stored `eval_config` | eval_run id | started (UTC) | audits read | audits from |
| --- | --- | --- | --- | --- | ---: | --- |
| repeat-* (labels as stored) | `repeat-1` | `scanners_only` | `0acf8910-6518-4690-8216-6ad1c449f350` | 2026-11-10 11:42 | 2 | rows (ambiguous label) |
| repeat-* (labels as stored) | `repeat-1` | `single_agent_no_tools` | `5bb1bf80-386a-49ad-b5e2-01de2cc08b30` | 2026-11-10 11:43 | 4 | rows |
| repeat-* (labels as stored) | `repeat-1` | `single_agent_tools` | `0f868c97-c88f-453d-9ace-20267238bc19` | 2026-11-10 11:49 | 4 | rows |
| repeat-* (labels as stored) | `repeat-1` | `multi_agent` | `95de84fa-1314-43d3-ab3b-d39ba3ca16ad` | 2026-11-10 11:55 | 4 | rows |
| repeat-* (labels as stored) | `repeat-1` | `scanners_plus_deterministic` | `97cae012-191b-4d93-91e9-e4a23e436b93` | 2026-11-10 12:11 | 3 | rows (ambiguous label) |
| repeat-* (labels as stored) | `repeat-2` | `scanners_only` | `1a0c2494-32fa-480d-b142-cf4013b0be87` | 2026-11-10 12:13 | 2 | rows (ambiguous label) |
| repeat-* (labels as stored) | `repeat-2` | `single_agent_no_tools` | `ed9d667d-7d52-488a-a2f6-a94aa24d9acd` | 2026-11-10 12:14 | 4 | rows |
| repeat-* (labels as stored) | `repeat-2` | `single_agent_tools` | `2c9e2cc1-1919-4162-a94c-e41dd39e690f` | 2026-11-10 12:18 | 4 | rows |
| repeat-* (labels as stored) | `repeat-2` | `multi_agent` | `c05105d2-ee4b-44a3-a99a-1e5cfa5af377` | 2026-11-10 12:26 | 4 | rows |
| repeat-* (labels as stored) | `repeat-2` | `scanners_plus_deterministic` | `dec2fbdc-752b-4b06-aefc-811faf5bc47f` | 2026-11-10 12:37 | 3 | rows (ambiguous label) |
| repeat-* (labels as stored) | `repeat-3` | `scanners_only` | `0dd88cd8-03d0-4771-8723-949c684833f5` | 2026-11-10 12:39 | 2 | rows (ambiguous label) |
| repeat-* (labels as stored) | `repeat-3` | `single_agent_no_tools` | `88c1dd7e-6f44-4729-8975-ae7a5de4b2e1` | 2026-11-10 12:40 | 4 | rows |
| repeat-* (labels as stored) | `repeat-3` | `single_agent_tools` | `2050f029-737b-40d7-9be7-3b989c1f89e4` | 2026-11-10 12:43 | 4 | rows |
| repeat-* (labels as stored) | `repeat-3` | `single_agent_tools` | `334b1720-fc77-4d00-bb56-ab7d3600407d` | 2026-11-10 12:55 | 4 | rows |
| repeat-* (labels as stored) | `repeat-3` | `multi_agent` | `386dd6df-27c1-451e-a0d5-db0864a3c148` | 2026-11-10 12:59 | 3 | rows (ambiguous label) |
| repeat-* (labels as stored) | `repeat-3` | `multi_agent` | `6f999178-5b61-4f72-8dad-4ba0515e604c` | 2026-11-10 13:02 | 3 | rows (ambiguous label) |
| repeat-* (labels as stored) | `repeat-3` | `scanners_plus_deterministic` | `dcf5e6a0-16e0-4861-8b31-48f84daca964` | 2026-11-10 13:08 | 3 | rows (ambiguous label) |
| repeat-* (labels as stored) | `repeat-3` | `scanners_plus_deterministic` | `eec9a838-c4da-4190-a953-2012fd84d244` | 2026-11-10 13:18 | 3 | rows (ambiguous label) |
| multi_agent baseline | `stage3-multi_agent-1` | `multi_agent` | `c6e114c2-19cb-4615-afb0-974d8a404817` | 2026-11-15 19:29 | 4 | rows |
| multi_agent baseline | `stage3-multi_agent-2` | `multi_agent` | `abb45616-7e79-4ebe-8d63-345294b2059a` | 2026-11-15 19:41 | 4 | rows+window |
| multi_agent baseline | `stage3-multi_agent-3` | `multi_agent` | `02e700a5-db10-4639-8b15-ec5bf4c5824d` | 2026-11-15 19:48 | 4 | rows+window |
| valid (pre-contract) | `valid-1` | `multi_agent_validated` | `ae3b2a5e-f4cb-4d0f-9167-f50e697801da` | 2026-11-13 17:32 | 4 | rows |
| valid (pre-contract) | `valid-2` | `multi_agent_validated` | `848d3f9b-a265-488f-8cf8-5809d1ad1d0d` | 2026-11-13 17:51 | 4 | rows+window |
| valid (pre-contract) | `valid-3` | `multi_agent_validated` | `fe718b04-7c40-4b82-a355-0e2b0786e2e0` | 2026-11-13 18:01 | 4 | rows+window |
| val2 (strict contract) | `val2-1` | `multi_agent_validated` | `56424325-2636-4b91-9dbe-e961c26b5309` | 2026-11-18 10:24 | 4 | rows+window |
| val2 (strict contract) | `val2-2` | `multi_agent_validated` | `3093e6a5-c304-4ed8-805c-3b9fe0cbb90a` | 2026-11-18 10:38 | 4 | rows |
| val2 (strict contract) | `val2-3` | `multi_agent_validated` | `17ae8010-1629-4308-bf17-7151ba018580` | 2026-11-18 11:20 | 4 | rows+window |
| val2 (strict contract) | `val2-1` | `multi_agent_validated` | `fe782e28-2eda-48d6-9d4f-334afdfab9f4` | 2026-11-18 11:44 | 4 | rows |
| val2 (strict contract) | `val2-2` | `multi_agent_validated` | `f6d1d661-f7ab-4d8f-b72f-8e75dc4b3b6b` | 2026-11-18 12:08 | 4 | rows |
| val2 (strict contract) | `val2-3` | `multi_agent_validated` | `b92564d6-a278-468a-851d-f41eca1cb5ba` | 2026-11-18 12:43 | 4 | rows |

## Stored metrics, val2 beside the earlier runs

Totals are `eval_runs.metrics.totals` exactly as the harness stored them. `matched` is planted issues the scored findings satisfied; a rejected finding is not scored, so it can never count here.

| group | label | configuration | precision | recall | false-positive rate | evidence accuracy | severity accuracy | cross-file recall | authorization (of 4) | matched | findings | cost | runtime (s) |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| repeat-* (labels as stored) | `repeat-1` | `scanners_only` | 1.000 | 0.667 | 0.000 | 1.000 | 1.000 | 0.000 | 0 | 12 | 25 | $0.0000 | 92.7 |
| repeat-* (labels as stored) | `repeat-1` | `single_agent_no_tools` | 0.857 | 0.667 | 0.143 | 1.000 | 1.000 | 0.000 | 0 | 12 | 33 | $0.0028 | 345.1 |
| repeat-* (labels as stored) | `repeat-1` | `single_agent_tools` | 0.706 | 0.667 | 0.294 | 1.000 | 1.000 | 0.000 | 0 | 12 | 30 | $0.0086 | 337.7 |
| repeat-* (labels as stored) | `repeat-1` | `multi_agent` | 0.842 | 0.889 | 0.158 | 0.938 | 0.938 | 0.667 | 2 | 16 | 32 | $0.0214 | 986.8 |
| repeat-* (labels as stored) | `repeat-1` | `scanners_plus_deterministic` | 1.000 | 0.778 | 0.000 | 0.929 | 1.000 | 0.333 | 0 | 14 | 27 | $0.0000 | 88.0 |
| repeat-* (labels as stored) | `repeat-2` | `scanners_only` | 1.000 | 0.667 | 0.000 | 1.000 | 1.000 | 0.000 | 0 | 12 | 25 | $0.0000 | 91.2 |
| repeat-* (labels as stored) | `repeat-2` | `single_agent_no_tools` | 0.923 | 0.667 | 0.077 | 1.000 | 1.000 | 0.000 | 0 | 12 | 32 | $0.0030 | 247.0 |
| repeat-* (labels as stored) | `repeat-2` | `single_agent_tools` | 0.750 | 0.667 | 0.250 | 1.000 | 1.000 | 0.000 | 0 | 12 | 29 | $0.0105 | 485.7 |
| repeat-* (labels as stored) | `repeat-2` | `multi_agent` | 0.941 | 0.889 | 0.059 | 0.938 | 0.938 | 0.667 | 2 | 16 | 30 | $0.0183 | 656.7 |
| repeat-* (labels as stored) | `repeat-2` | `scanners_plus_deterministic` | 1.000 | 0.778 | 0.000 | 0.929 | 1.000 | 0.333 | 0 | 14 | 27 | $0.0000 | 67.7 |
| repeat-* (labels as stored) | `repeat-3` | `scanners_only` | 1.000 | 0.667 | 0.000 | 1.000 | 1.000 | 0.000 | 0 | 12 | 25 | $0.0000 | 61.8 |
| repeat-* (labels as stored) | `repeat-3` | `single_agent_no_tools` | 0.875 | 0.778 | 0.125 | 1.000 | 0.929 | 0.333 | 1 | 14 | 35 | $0.0030 | 207.3 |
| repeat-* (labels as stored) | `repeat-3` | `single_agent_tools` | 0.700 | 0.778 | 0.300 | 1.000 | 1.000 | 0.333 | 2 | 14 | 34 | $0.0107 | 967.0 |
| repeat-* (labels as stored) | `repeat-3` | `single_agent_tools` | 0.632 | 0.667 | 0.368 | 1.000 | 1.000 | 0.000 | 0 | 12 | 32 | $0.0097 | 394.4 |
| repeat-* (labels as stored) | `repeat-3` | `multi_agent` | 1.000 | 0.944 | 0.000 | 0.941 | 0.882 | 0.833 | 3 | 17 | 30 | $0.0218 | 521.7 |
| repeat-* (labels as stored) | `repeat-3` | `multi_agent` | 0.938 | 0.833 | 0.062 | 0.933 | 0.933 | 0.500 | 1 | 15 | 29 | $0.0253 | 939.2 |
| repeat-* (labels as stored) | `repeat-3` | `scanners_plus_deterministic` | 1.000 | 0.778 | 0.000 | 0.929 | 1.000 | 0.333 | 0 | 14 | 27 | $0.0000 | 69.3 |
| repeat-* (labels as stored) | `repeat-3` | `scanners_plus_deterministic` | 1.000 | 0.778 | 0.000 | 0.929 | 1.000 | 0.333 | 0 | 14 | 27 | $0.0000 | 60.3 |
| multi_agent baseline | `stage3-multi_agent-1` | `multi_agent` | 0.895 | 0.944 | 0.105 | 0.941 | 0.941 | 0.833 | 3 | 17 | 35 | $0.0226 | 715.5 |
| multi_agent baseline | `stage3-multi_agent-2` | `multi_agent` | 1.000 | 0.778 | 0.000 | 0.929 | 1.000 | 0.333 | 0 | 14 | 27 | $0.0008 | 432.6 |
| multi_agent baseline | `stage3-multi_agent-3` | `multi_agent` | 1.000 | 0.778 | 0.000 | 0.929 | 1.000 | 0.333 | 0 | 14 | 27 | $0.0000 | 389.4 |
| valid (pre-contract) | `valid-1` | `multi_agent_validated` | 0.941 | 0.889 | 0.059 | 0.938 | 1.000 | 0.667 | 2 | 16 | 30 | $0.0179 | 1134.5 |
| valid (pre-contract) | `valid-2` | `multi_agent_validated` | 1.000 | 0.889 | 0.000 | 0.938 | 0.938 | 0.667 | 2 | 16 | 29 | $0.0193 | 586.1 |
| valid (pre-contract) | `valid-3` | `multi_agent_validated` | 0.938 | 0.833 | 0.062 | 0.933 | 0.867 | 0.500 | 1 | 15 | 29 | $0.0231 | 958.2 |
| val2 (strict contract) | `val2-1` | `multi_agent_validated` | 1.000 | 0.944 | 0.000 | 0.941 | 0.941 | 0.833 | 3 | 17 | 32 | $0.0254 | 794.3 |
| val2 (strict contract) | `val2-2` | `multi_agent_validated` | 0.944 | 0.944 | 0.056 | 0.941 | 0.941 | 0.833 | 3 | 17 | 33 | $0.0231 | 2502.4 |
| val2 (strict contract) | `val2-3` | `multi_agent_validated` | 1.000 | 0.944 | 0.000 | 0.941 | 1.000 | 0.833 | 3 | 17 | 32 | $0.1466 | 5338.3 |
| val2 (strict contract) | `val2-1` | `multi_agent_validated` | 1.000 | 1.000 | 0.000 | 0.944 | 0.889 | 1.000 | 4 | 18 | 33 | $0.0299 | 1474.1 |
| val2 (strict contract) | `val2-2` | `multi_agent_validated` | 0.889 | 0.889 | 0.111 | 0.938 | 0.938 | 0.667 | 2 | 16 | 33 | $0.0241 | 2118.3 |
| val2 (strict contract) | `val2-3` | `multi_agent_validated` | 0.842 | 0.889 | 0.158 | 0.938 | 0.938 | 0.667 | 2 | 16 | 34 | $0.0376 | 2196.8 |

## Authorization recall (the four planted access-control gaps)

The four authorization issues are the ones `benchmark/report.py` names (`adv-delete-authz-02`, `adv-export-authz-01`, `js-authz-01`, `py-authz-01`). Three runs can match four each, so a group has twelve cells; the count is matched cells over twelve, read from each row's stored `metrics.totals.matched_ids`.

| group | runs | matched cells | recall |
| --- | --- | ---: | ---: |
| repeat-* (multi_agent rows) | `repeat-1`, `repeat-2`, `repeat-3` | 5 / 12 | 0.417 |
| multi_agent baseline | `stage3-multi_agent-1`, `stage3-multi_agent-2`, `stage3-multi_agent-3` | 3 / 12 | 0.250 |
| valid (pre-contract) | `valid-1`, `valid-2`, `valid-3` | 5 / 12 | 0.417 |
| val2 (strict contract) | `val2-1`, `val2-2`, `val2-3` | 8 / 12 | 0.667 |

## Validation outcome per run

The first columns are the validator's own counts, summed from `eval_runs.metrics.agents.*.validation`. The last three are the stored `findings.verification_status` of every finding in the run's audits, which is what the scored rows and the report actually see.

| label | configuration | checked | verified | rejected | unresolved | model calls | validator cost | stored verified | stored rejected | stored unresolved | other statuses |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `repeat-1` | scanners_only | 0 | 0 | 0 | 0 | 0 | $0.0000 | 0 | 0 | 0 | scanner_confirmed=17 |
| `repeat-1` | single_agent_no_tools | 0 | 0 | 0 | 0 | 0 | $0.0000 | 0 | 0 | 0 | ai_suggested=8, scanner_confirmed=25 |
| `repeat-1` | single_agent_tools | 0 | 0 | 0 | 0 | 0 | $0.0000 | 0 | 0 | 0 | ai_suggested=5, scanner_confirmed=25 |
| `repeat-1` | multi_agent | 0 | 0 | 0 | 0 | 0 | $0.0000 | 0 | 0 | 0 | ai_suggested=7, scanner_confirmed=25 |
| `repeat-1` | scanners_plus_deterministic | 0 | 0 | 0 | 0 | 0 | $0.0000 | 0 | 0 | 0 | ai_suggested=2, scanner_confirmed=25 |
| `repeat-2` | scanners_only | 0 | 0 | 0 | 0 | 0 | $0.0000 | 0 | 0 | 0 | scanner_confirmed=17 |
| `repeat-2` | single_agent_no_tools | 0 | 0 | 0 | 0 | 0 | $0.0000 | 0 | 0 | 0 | ai_suggested=7, scanner_confirmed=25 |
| `repeat-2` | single_agent_tools | 0 | 0 | 0 | 0 | 0 | $0.0000 | 0 | 0 | 0 | ai_suggested=4, scanner_confirmed=25 |
| `repeat-2` | multi_agent | 0 | 0 | 0 | 0 | 0 | $0.0000 | 0 | 0 | 0 | ai_suggested=5, scanner_confirmed=25 |
| `repeat-2` | scanners_plus_deterministic | 0 | 0 | 0 | 0 | 0 | $0.0000 | 0 | 0 | 0 | ai_suggested=2, scanner_confirmed=25 |
| `repeat-3` | scanners_only | 0 | 0 | 0 | 0 | 0 | $0.0000 | 0 | 0 | 0 | scanner_confirmed=17 |
| `repeat-3` | single_agent_no_tools | 0 | 0 | 0 | 0 | 0 | $0.0000 | 0 | 0 | 0 | ai_suggested=10, scanner_confirmed=25 |
| `repeat-3` | single_agent_tools | 0 | 0 | 0 | 0 | 0 | $0.0000 | 0 | 0 | 0 | ai_suggested=9, scanner_confirmed=25 |
| `repeat-3` | single_agent_tools | 0 | 0 | 0 | 0 | 0 | $0.0000 | 0 | 0 | 0 | ai_suggested=7, scanner_confirmed=25 |
| `repeat-3` | multi_agent | 0 | 0 | 0 | 0 | 0 | $0.0000 | 0 | 0 | 0 | ai_suggested=5, scanner_confirmed=25 |
| `repeat-3` | multi_agent | 0 | 0 | 0 | 0 | 0 | $0.0000 | 0 | 0 | 0 | ai_suggested=4, scanner_confirmed=25 |
| `repeat-3` | scanners_plus_deterministic | 0 | 0 | 0 | 0 | 0 | $0.0000 | 0 | 0 | 0 | ai_suggested=2, scanner_confirmed=25 |
| `repeat-3` | scanners_plus_deterministic | 0 | 0 | 0 | 0 | 0 | $0.0000 | 0 | 0 | 0 | ai_suggested=2, scanner_confirmed=25 |
| `stage3-multi_agent-1` | multi_agent | 0 | 0 | 0 | 0 | 0 | $0.0000 | 0 | 0 | 0 | ai_suggested=10, scanner_confirmed=25 |
| `stage3-multi_agent-2` | multi_agent | 0 | 0 | 0 | 0 | 0 | $0.0000 | 0 | 0 | 0 | ai_suggested=2, scanner_confirmed=25 |
| `stage3-multi_agent-3` | multi_agent | 0 | 0 | 0 | 0 | 0 | $0.0000 | 0 | 0 | 0 | ai_suggested=2, scanner_confirmed=25 |
| `valid-1` | multi_agent_validated | 6 | 5 | 1 | 0 | 0 | $0.0000 | 5 | 1 | 0 | scanner_confirmed=25 |
| `valid-2` | multi_agent_validated | 5 | 4 | 1 | 0 | 0 | $0.0000 | 4 | 1 | 0 | scanner_confirmed=25 |
| `valid-3` | multi_agent_validated | 8 | 4 | 4 | 0 | 0 | $0.0000 | 4 | 4 | 0 | scanner_confirmed=25 |
| `val2-1` | multi_agent_validated | 9 | 5 | 1 | 3 | 9 | $0.0009 | 5 | 1 | 3 | scanner_confirmed=24 |
| `val2-2` | multi_agent_validated | 9 | 6 | 0 | 3 | 9 | $0.0009 | 6 | 0 | 3 | scanner_confirmed=24 |
| `val2-3` | multi_agent_validated | 8 | 5 | 0 | 3 | 8 | $0.0008 | 5 | 0 | 3 | scanner_confirmed=24 |
| `val2-1` | multi_agent_validated | 11 | 6 | 2 | 3 | 11 | $0.0011 | 6 | 2 | 3 | scanner_confirmed=24 |
| `val2-2` | multi_agent_validated | 12 | 6 | 3 | 3 | 12 | $0.0012 | 6 | 3 | 3 | scanner_confirmed=24 |
| `val2-3` | multi_agent_validated | 10 | 7 | 0 | 3 | 10 | $0.0010 | 7 | 0 | 3 | scanner_confirmed=24 |

## Rejected findings

Every finding stored as `rejected_false_positive`, with the semantic check that produced the verdict. `matches` names the planted issue the finding's category, file and lines would have satisfied under `benchmark/harness/matching.py`; a rejected finding that matches was real signal the validator threw away. Raw replies are truncated to 320 characters here and stored in full in `validation_checks.raw_reply`.

### valid (pre-contract)

#### `valid-1` (multi_agent_validated, `ae3b2a5e-f4cb-4d0f-9167-f50e697801da`)

| repo | category | finding | location | verdict | quote | matches | raw reply |
| --- | --- | --- | --- | --- | --- | --- | --- |
| adversarial_app | security | `a5ba9112` The handle_export function in app/routes/export.py does not check the caller | `app/routes/export.py:14-19` | — | — | `adv-export-authz-01` | — |

#### `valid-2` (multi_agent_validated, `848d3f9b-a265-488f-8cf8-5809d1ad1d0d`)

| repo | category | finding | location | verdict | quote | matches | raw reply |
| --- | --- | --- | --- | --- | --- | --- | --- |
| adversarial_app | security | `3f7a03fe` The handle_export function in app/routes/export.py does not authenticate the caller | `app/routes/export.py:14-19` | — | — | `adv-export-authz-01` | — |

#### `valid-3` (multi_agent_validated, `fe718b04-7c40-4b82-a355-0e2b0786e2e0`)

| repo | category | finding | location | verdict | quote | matches | raw reply |
| --- | --- | --- | --- | --- | --- | --- | --- |
| adversarial_app | security | `d13bc47b` The handle_export function in app/routes/export.py does not authenticate the caller | `app/routes/export.py:14-19` | — | — | `adv-export-authz-01` | — |
| clean_app | security | `2c4c0486` The handle_read function in app/routes.py does not authenticate the caller or check that … | `app/routes.py:25-34` | — | — | — | — |
| clean_app | security | `644e0a08` The handle_search function in app/routes.py does not authenticate the caller or check tha… | `app/routes.py:16-22` | — | — | — | — |
| js_app | security | `46751046` No authentication for user data access | `src/routes/users.js:9-12` | — | — | — | — |

### val2 (strict contract)

#### `val2-1` (multi_agent_validated, `56424325-2636-4b91-9dbe-e961c26b5309`)

| repo | category | finding | location | verdict | quote | matches | raw reply |
| --- | --- | --- | --- | --- | --- | --- | --- |
| adversarial_app | security | `c1272a39` The handle_read_document function in app/routes/documents.py does not authenticate the ca… | `app/routes/documents.py:11-20` | contradicted | user = current_user(session) | — | {"verdict": "contradicted", "quote": "user = current_user(session)"} |

#### `val2-1` (multi_agent_validated, `fe782e28-2eda-48d6-9d4f-334afdfab9f4`)

| repo | category | finding | location | verdict | quote | matches | raw reply |
| --- | --- | --- | --- | --- | --- | --- | --- |
| clean_app | security | `8be75959` The handle_delete function in app/routes.py does not authenticate the caller or check tha… | `app/routes.py:37-42` | contradicted | user: User = current_user(session) | — | {"verdict": "contradicted", "quote": "user: User = current_user(session)"} |
| clean_app | security | `2660464f` The handle_read function in app/routes.py does not authenticate the caller or check that … | `app/routes.py:25-34` | contradicted | user: User = current_user(session) | — | {"verdict": "contradicted", "quote": "user: User = current_user(session)"} |

#### `val2-2` (multi_agent_validated, `f6d1d661-f7ab-4d8f-b72f-8e75dc4b3b6b`)

| repo | category | finding | location | verdict | quote | matches | raw reply |
| --- | --- | --- | --- | --- | --- | --- | --- |
| adversarial_app | security | `5a2713fb` The handle_delete_document function in app/routes/documents.py does not authenticate the … | `app/routes/documents.py:23-28` | contradicted | current_user(session) | `adv-delete-authz-02` | {"verdict": "contradicted", "quote": "current_user(session)"} |
| adversarial_app | security | `ab619474` The handle_read_document function in app/routes/documents.py does not authenticate the ca… | `app/routes/documents.py:11-20` | contradicted | user = current_user(session) | — | {"verdict": "contradicted", "quote": "user = current_user(session)"} |
| js_app | security | `a8f30af6` Missing authentication for user data access | `src/routes/users.js:9-12` | contradicted | requireAuth | — | {"verdict": "contradicted", "quote": "requireAuth"} |

## Rejected findings that matched a planted issue

The join is on the finding id: `validation_checks.finding_id` identifies the checked finding, and that finding's stored evidence is tested against `benchmark/expected.json` with the harness's own rule (category, file, overlapping lines, plus the cycle rule).

| label | configuration | rejected findings stored | with a semantic check | with a quote | matching a planted issue | issue ids |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| `repeat-1` | scanners_only | 0 | 0 | 0 | 0 | — |
| `repeat-1` | single_agent_no_tools | 0 | 0 | 0 | 0 | — |
| `repeat-1` | single_agent_tools | 0 | 0 | 0 | 0 | — |
| `repeat-1` | multi_agent | 0 | 0 | 0 | 0 | — |
| `repeat-1` | scanners_plus_deterministic | 0 | 0 | 0 | 0 | — |
| `repeat-2` | scanners_only | 0 | 0 | 0 | 0 | — |
| `repeat-2` | single_agent_no_tools | 0 | 0 | 0 | 0 | — |
| `repeat-2` | single_agent_tools | 0 | 0 | 0 | 0 | — |
| `repeat-2` | multi_agent | 0 | 0 | 0 | 0 | — |
| `repeat-2` | scanners_plus_deterministic | 0 | 0 | 0 | 0 | — |
| `repeat-3` | scanners_only | 0 | 0 | 0 | 0 | — |
| `repeat-3` | single_agent_no_tools | 0 | 0 | 0 | 0 | — |
| `repeat-3` | single_agent_tools | 0 | 0 | 0 | 0 | — |
| `repeat-3` | single_agent_tools | 0 | 0 | 0 | 0 | — |
| `repeat-3` | multi_agent | 0 | 0 | 0 | 0 | — |
| `repeat-3` | multi_agent | 0 | 0 | 0 | 0 | — |
| `repeat-3` | scanners_plus_deterministic | 0 | 0 | 0 | 0 | — |
| `repeat-3` | scanners_plus_deterministic | 0 | 0 | 0 | 0 | — |
| `stage3-multi_agent-1` | multi_agent | 0 | 0 | 0 | 0 | — |
| `stage3-multi_agent-2` | multi_agent | 0 | 0 | 0 | 0 | — |
| `stage3-multi_agent-3` | multi_agent | 0 | 0 | 0 | 0 | — |
| `valid-1` | multi_agent_validated | 1 | 0 | 0 | 1 | `adv-export-authz-01` |
| `valid-2` | multi_agent_validated | 1 | 0 | 0 | 1 | `adv-export-authz-01` |
| `valid-3` | multi_agent_validated | 4 | 0 | 0 | 1 | `adv-export-authz-01` |
| `val2-1` | multi_agent_validated | 1 | 1 | 1 | 0 | — |
| `val2-2` | multi_agent_validated | 0 | 0 | 0 | 0 | — |
| `val2-3` | multi_agent_validated | 0 | 0 | 0 | 0 | — |
| `val2-1` | multi_agent_validated | 2 | 2 | 2 | 0 | — |
| `val2-2` | multi_agent_validated | 3 | 3 | 3 | 1 | `adv-delete-authz-02` |
| `val2-3` | multi_agent_validated | 0 | 0 | 0 | 0 | — |

## Model calls that failed or were rate limited

`agent_runs` stores one row per gateway attempt with its status, so a failed or rate-limited call is a row whose status is not `succeeded`. A validator call that failed leaves its check row with a NULL `raw_reply`, and the reason is in the `finding_events` note the validator wrote.

- `repeat-1` (scanners_only): 0 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
- `repeat-1` (single_agent_no_tools): 0 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
- `repeat-1` (single_agent_tools): 0 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
- `repeat-1` (multi_agent): 0 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
- `repeat-1` (scanners_plus_deterministic): 0 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
- `repeat-2` (scanners_only): 0 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
- `repeat-2` (single_agent_no_tools): 0 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
- `repeat-2` (single_agent_tools): 0 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
- `repeat-2` (multi_agent): 0 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
- `repeat-2` (scanners_plus_deterministic): 0 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
- `repeat-3` (scanners_only): 0 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
- `repeat-3` (single_agent_no_tools): 0 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
- `repeat-3` (single_agent_tools): 0 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
- `repeat-3` (single_agent_tools): 0 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
- `repeat-3` (multi_agent): 0 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
- `repeat-3` (multi_agent): 1 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
  - agent `security` model `agent-default` status `failed` × 1
- `repeat-3` (scanners_plus_deterministic): 0 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
- `repeat-3` (scanners_plus_deterministic): 0 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
- `stage3-multi_agent-1` (multi_agent): 0 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
- `stage3-multi_agent-2` (multi_agent): 216 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
  - agent `architecture` model `agent-default` status `failed` × 36
  - agent `architecture` model `gpt-4o` status `failed` × 36
  - agent `compliance` model `agent-default` status `failed` × 36
  - agent `compliance` model `gpt-4o` status `failed` × 36
  - agent `security` model `agent-default` status `failed` × 36
  - agent `security` model `gpt-4o` status `failed` × 36
- `stage3-multi_agent-3` (multi_agent): 216 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
  - agent `architecture` model `agent-default` status `failed` × 36
  - agent `architecture` model `gpt-4o` status `failed` × 36
  - agent `compliance` model `agent-default` status `failed` × 36
  - agent `compliance` model `gpt-4o` status `failed` × 36
  - agent `security` model `agent-default` status `failed` × 36
  - agent `security` model `gpt-4o` status `failed` × 36
- `valid-1` (multi_agent_validated): 2 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
  - agent `security` model `agent-default` status `failed` × 2
- `valid-2` (multi_agent_validated): 0 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
- `valid-3` (multi_agent_validated): 0 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
- `val2-1` (multi_agent_validated): 0 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
- `val2-2` (multi_agent_validated): 0 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
- `val2-3` (multi_agent_validated): 35 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
  - agent `architecture` model `agent-default` status `failed` × 7
  - agent `architecture` model `gpt-4o` status `failed` × 4
  - agent `compliance` model `agent-default` status `failed` × 9
  - agent `compliance` model `gpt-4o` status `failed` × 9
  - agent `security` model `agent-default` status `failed` × 4
  - agent `security` model `gpt-4o` status `failed` × 2
- `val2-1` (multi_agent_validated): 0 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
- `val2-2` (multi_agent_validated): 2 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
  - agent `security` model `agent-default` status `failed` × 2
- `val2-3` (multi_agent_validated): 4 failed `agent_runs` row(s), 0 validator call(s) failed, 0 check row(s) without a stored reply.
  - agent `architecture` model `agent-default` status `failed` × 1
  - agent `security` model `agent-default` status `failed` × 3

## Integrity checks

| check | result |
| --- | --- |
| multi_agent baseline: one stored `eval_runs` row per label | PASS |
| valid (pre-contract): one stored `eval_runs` row per label | PASS |
| val2 (strict contract): one stored `eval_runs` row per label (duplicated: val2-1, val2-2, val2-3) | FAIL |
| `repeat-1` (scanners_only): 2 audit(s) read from the row's own links; an ambiguous label cannot be resolved further (rows (ambiguous label)) | PASS |
| `repeat-1` (single_agent_no_tools): 4 audit(s) read from the row's own links; an ambiguous label cannot be resolved further (rows) | PASS |
| `repeat-1` (single_agent_tools): 4 audit(s) read from the row's own links; an ambiguous label cannot be resolved further (rows) | PASS |
| `repeat-1` (multi_agent): 4 audit(s) read from the row's own links; an ambiguous label cannot be resolved further (rows) | PASS |
| `repeat-1` (scanners_plus_deterministic): 3 audit(s) read from the row's own links; an ambiguous label cannot be resolved further (rows (ambiguous label)) | PASS |
| `repeat-2` (scanners_only): 2 audit(s) read from the row's own links; an ambiguous label cannot be resolved further (rows (ambiguous label)) | PASS |
| `repeat-2` (single_agent_no_tools): 4 audit(s) read from the row's own links; an ambiguous label cannot be resolved further (rows) | PASS |
| `repeat-2` (single_agent_tools): 4 audit(s) read from the row's own links; an ambiguous label cannot be resolved further (rows) | PASS |
| `repeat-2` (multi_agent): 4 audit(s) read from the row's own links; an ambiguous label cannot be resolved further (rows) | PASS |
| `repeat-2` (scanners_plus_deterministic): 3 audit(s) read from the row's own links; an ambiguous label cannot be resolved further (rows (ambiguous label)) | PASS |
| `repeat-3` (scanners_only): 2 audit(s) read from the row's own links; an ambiguous label cannot be resolved further (rows (ambiguous label)) | PASS |
| `repeat-3` (single_agent_no_tools): 4 audit(s) read from the row's own links; an ambiguous label cannot be resolved further (rows) | PASS |
| `repeat-3` (single_agent_tools): 4 audit(s) read from the row's own links; an ambiguous label cannot be resolved further (rows) | PASS |
| `repeat-3` (single_agent_tools): 4 audit(s) read from the row's own links; an ambiguous label cannot be resolved further (rows) | PASS |
| `repeat-3` (multi_agent): 3 audit(s) read from the row's own links; an ambiguous label cannot be resolved further (rows (ambiguous label)) | PASS |
| `repeat-3` (multi_agent): 3 audit(s) read from the row's own links; an ambiguous label cannot be resolved further (rows (ambiguous label)) | PASS |
| `repeat-3` (scanners_plus_deterministic): 3 audit(s) read from the row's own links; an ambiguous label cannot be resolved further (rows (ambiguous label)) | PASS |
| `repeat-3` (scanners_plus_deterministic): 3 audit(s) read from the row's own links; an ambiguous label cannot be resolved further (rows (ambiguous label)) | PASS |
| `stage3-multi_agent-1` (multi_agent): 4 audit(s) read, expected 4 (rows) | PASS |
| `stage3-multi_agent-2` (multi_agent): 4 audit(s) read, expected 4 (rows+window) | PASS |
| `stage3-multi_agent-3` (multi_agent): 4 audit(s) read, expected 4 (rows+window) | PASS |
| `valid-1` (multi_agent_validated): 4 audit(s) read, expected 4 (rows) | PASS |
| `valid-2` (multi_agent_validated): 4 audit(s) read, expected 4 (rows+window) | PASS |
| `valid-3` (multi_agent_validated): 4 audit(s) read, expected 4 (rows+window) | PASS |
| `val2-1` (multi_agent_validated): 4 audit(s) read, expected 4 (rows+window) | PASS |
| `val2-2` (multi_agent_validated): 4 audit(s) read, expected 4 (rows) | PASS |
| `val2-3` (multi_agent_validated): 4 audit(s) read, expected 4 (rows+window) | PASS |
| `val2-1` (multi_agent_validated): 4 audit(s) read, expected 4 (rows) | PASS |
| `val2-2` (multi_agent_validated): 4 audit(s) read, expected 4 (rows) | PASS |
| `val2-3` (multi_agent_validated): 4 audit(s) read, expected 4 (rows) | PASS |
| `valid-1`: every stored verdict is one of supported, contradicted, cannot_tell | PASS |
| `valid-1`: every rejected finding's check says `contradicted` | PASS |
| `valid-1`: every rejected finding's check stores its quote | PASS |
| `valid-2`: every stored verdict is one of supported, contradicted, cannot_tell | PASS |
| `valid-2`: every rejected finding's check says `contradicted` | PASS |
| `valid-2`: every rejected finding's check stores its quote | PASS |
| `valid-3`: every stored verdict is one of supported, contradicted, cannot_tell | PASS |
| `valid-3`: every rejected finding's check says `contradicted` | PASS |
| `valid-3`: every rejected finding's check stores its quote | PASS |
| `val2-1`: every stored verdict is one of supported, contradicted, cannot_tell | PASS |
| `val2-1`: every rejected finding's check says `contradicted` | PASS |
| `val2-1`: every rejected finding's check stores its quote | PASS |
| `val2-2`: every stored verdict is one of supported, contradicted, cannot_tell | PASS |
| `val2-2`: every rejected finding's check says `contradicted` | PASS |
| `val2-2`: every rejected finding's check stores its quote | PASS |
| `val2-3`: every stored verdict is one of supported, contradicted, cannot_tell | PASS |
| `val2-3`: every rejected finding's check says `contradicted` | PASS |
| `val2-3`: every rejected finding's check stores its quote | PASS |
| `val2-1`: every stored verdict is one of supported, contradicted, cannot_tell | PASS |
| `val2-1`: every rejected finding's check says `contradicted` | PASS |
| `val2-1`: every rejected finding's check stores its quote | PASS |
| `val2-2`: every stored verdict is one of supported, contradicted, cannot_tell | PASS |
| `val2-2`: every rejected finding's check says `contradicted` | PASS |
| `val2-2`: every rejected finding's check stores its quote | PASS |
| `val2-3`: every stored verdict is one of supported, contradicted, cannot_tell | PASS |
| `val2-3`: every rejected finding's check says `contradicted` | PASS |
| `val2-3`: every rejected finding's check stores its quote | PASS |
| `repeat-1` (scanners_only): 18 stored result row(s), expected 18 | PASS |
| `repeat-1` (scanners_only): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `repeat-1` (single_agent_no_tools): 18 stored result row(s), expected 18 | PASS |
| `repeat-1` (single_agent_no_tools): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `repeat-1` (single_agent_tools): 18 stored result row(s), expected 18 | PASS |
| `repeat-1` (single_agent_tools): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `repeat-1` (multi_agent): 18 stored result row(s), expected 18 | PASS |
| `repeat-1` (multi_agent): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `repeat-1` (scanners_plus_deterministic): 18 stored result row(s), expected 18 | PASS |
| `repeat-1` (scanners_plus_deterministic): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `repeat-2` (scanners_only): 18 stored result row(s), expected 18 | PASS |
| `repeat-2` (scanners_only): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `repeat-2` (single_agent_no_tools): 18 stored result row(s), expected 18 | PASS |
| `repeat-2` (single_agent_no_tools): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `repeat-2` (single_agent_tools): 18 stored result row(s), expected 18 | PASS |
| `repeat-2` (single_agent_tools): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `repeat-2` (multi_agent): 18 stored result row(s), expected 18 | PASS |
| `repeat-2` (multi_agent): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `repeat-2` (scanners_plus_deterministic): 18 stored result row(s), expected 18 | PASS |
| `repeat-2` (scanners_plus_deterministic): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `repeat-3` (scanners_only): 18 stored result row(s), expected 18 | PASS |
| `repeat-3` (scanners_only): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `repeat-3` (single_agent_no_tools): 18 stored result row(s), expected 18 | PASS |
| `repeat-3` (single_agent_no_tools): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `repeat-3` (single_agent_tools): 18 stored result row(s), expected 18 | PASS |
| `repeat-3` (single_agent_tools): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `repeat-3` (single_agent_tools): 18 stored result row(s), expected 18 | PASS |
| `repeat-3` (single_agent_tools): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `repeat-3` (multi_agent): 18 stored result row(s), expected 18 | PASS |
| `repeat-3` (multi_agent): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `repeat-3` (multi_agent): 18 stored result row(s), expected 18 | PASS |
| `repeat-3` (multi_agent): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `repeat-3` (scanners_plus_deterministic): 18 stored result row(s), expected 18 | PASS |
| `repeat-3` (scanners_plus_deterministic): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `repeat-3` (scanners_plus_deterministic): 18 stored result row(s), expected 18 | PASS |
| `repeat-3` (scanners_plus_deterministic): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `stage3-multi_agent-1` (multi_agent): 18 stored result row(s), expected 18 | PASS |
| `stage3-multi_agent-1` (multi_agent): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `stage3-multi_agent-2` (multi_agent): 18 stored result row(s), expected 18 | PASS |
| `stage3-multi_agent-2` (multi_agent): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `stage3-multi_agent-3` (multi_agent): 18 stored result row(s), expected 18 | PASS |
| `stage3-multi_agent-3` (multi_agent): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `valid-1` (multi_agent_validated): 18 stored result row(s), expected 18 | PASS |
| `valid-1` (multi_agent_validated): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `valid-2` (multi_agent_validated): 18 stored result row(s), expected 18 | PASS |
| `valid-2` (multi_agent_validated): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `valid-3` (multi_agent_validated): 18 stored result row(s), expected 18 | PASS |
| `valid-3` (multi_agent_validated): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `val2-1` (multi_agent_validated): 18 stored result row(s), expected 18 | PASS |
| `val2-1` (multi_agent_validated): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `val2-2` (multi_agent_validated): 18 stored result row(s), expected 18 | PASS |
| `val2-2` (multi_agent_validated): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `val2-3` (multi_agent_validated): 18 stored result row(s), expected 18 | PASS |
| `val2-3` (multi_agent_validated): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `val2-1` (multi_agent_validated): 18 stored result row(s), expected 18 | PASS |
| `val2-1` (multi_agent_validated): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `val2-2` (multi_agent_validated): 18 stored result row(s), expected 18 | PASS |
| `val2-2` (multi_agent_validated): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `val2-3` (multi_agent_validated): 18 stored result row(s), expected 18 | PASS |
| `val2-3` (multi_agent_validated): every matched finding id belongs to this run (0 orphan(s)) | PASS |
| `repeat-1` (scanners_only): no rejected finding is scored as detected (0) | PASS |
| `repeat-1` (single_agent_no_tools): no rejected finding is scored as detected (0) | PASS |
| `repeat-1` (single_agent_tools): no rejected finding is scored as detected (0) | PASS |
| `repeat-1` (multi_agent): no rejected finding is scored as detected (0) | PASS |
| `repeat-1` (scanners_plus_deterministic): no rejected finding is scored as detected (0) | PASS |
| `repeat-2` (scanners_only): no rejected finding is scored as detected (0) | PASS |
| `repeat-2` (single_agent_no_tools): no rejected finding is scored as detected (0) | PASS |
| `repeat-2` (single_agent_tools): no rejected finding is scored as detected (0) | PASS |
| `repeat-2` (multi_agent): no rejected finding is scored as detected (0) | PASS |
| `repeat-2` (scanners_plus_deterministic): no rejected finding is scored as detected (0) | PASS |
| `repeat-3` (scanners_only): no rejected finding is scored as detected (0) | PASS |
| `repeat-3` (single_agent_no_tools): no rejected finding is scored as detected (0) | PASS |
| `repeat-3` (single_agent_tools): no rejected finding is scored as detected (0) | PASS |
| `repeat-3` (single_agent_tools): no rejected finding is scored as detected (0) | PASS |
| `repeat-3` (multi_agent): no rejected finding is scored as detected (0) | PASS |
| `repeat-3` (multi_agent): no rejected finding is scored as detected (0) | PASS |
| `repeat-3` (scanners_plus_deterministic): no rejected finding is scored as detected (0) | PASS |
| `repeat-3` (scanners_plus_deterministic): no rejected finding is scored as detected (0) | PASS |
| `stage3-multi_agent-1` (multi_agent): no rejected finding is scored as detected (0) | PASS |
| `stage3-multi_agent-2` (multi_agent): no rejected finding is scored as detected (0) | PASS |
| `stage3-multi_agent-3` (multi_agent): no rejected finding is scored as detected (0) | PASS |
| `valid-1` (multi_agent_validated): no rejected finding is scored as detected (0) | PASS |
| `valid-2` (multi_agent_validated): no rejected finding is scored as detected (0) | PASS |
| `valid-3` (multi_agent_validated): no rejected finding is scored as detected (0) | PASS |
| `val2-1` (multi_agent_validated): no rejected finding is scored as detected (0) | PASS |
| `val2-2` (multi_agent_validated): no rejected finding is scored as detected (0) | PASS |
| `val2-3` (multi_agent_validated): no rejected finding is scored as detected (0) | PASS |
| `val2-1` (multi_agent_validated): no rejected finding is scored as detected (0) | PASS |
| `val2-2` (multi_agent_validated): no rejected finding is scored as detected (0) | PASS |
| `val2-3` (multi_agent_validated): no rejected finding is scored as detected (0) | PASS |

## Notes on method

- A rejected finding is excluded from scoring by `_load_findings` in `benchmark/harness/runner.py`, so it can lower recall and never raises precision; the join above is what measures whether a rejection removed real signal.
- `validation_checks` stores one row per semantic attempt that had a readable cited block: the cited file and line range, the verdict, the quote, the full masked reply, the latency and the cost. A call that failed keeps its row with a NULL `raw_reply`.
- A deterministic rejection (missing file, snippet not at the cited lines) is final and makes no model call, so it has no `validation_checks` row; its reason is in `finding_events`.
- The `val2-1..3` labels are stored twice: once by an earlier session whose process was interrupted, and once by the re-run that produced this report. The Runs table keeps both, by `eval_run id` and start time, rather than pretending either one was overwritten.
- `val2-*` audits are read back through the check rows the harness linked by `eval_run_id`; `valid-*` runs predate that table, so their audits come from the findings their `eval_results`/`eval_extras` rows name, with the configuration's audits between run starts as the fallback the Runs table reports.
- `repeat-*` labels were reused across different configurations on 2026-11-10, so those rows are listed as stored, each with the configuration it actually ran; `stage3-multi_agent-1..3` is the clean three-run `multi_agent` comparison.

