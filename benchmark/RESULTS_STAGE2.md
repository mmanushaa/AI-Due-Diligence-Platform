# Stage 2 benchmark results

Benchmark version 2. Three runs per configuration, in the order the runs were executed, against the four-repository fixture set (`clean_app`, `js_app`, `python_app`, `adversarial_app`; 18 planted issues in total).

Configuration names are the stored `eval_config` values. `single_agent_no_tools` is the configuration the roadmap calls *single_agent*.

Each run was produced from the repository root with `python -m benchmark.harness --config <configuration> --label stage2-<configuration>-<n>`.

## Average metrics per configuration

Mean of the three runs. Totals are summed over the four repositories in each run, then averaged.

| configuration | runs | recall | precision | false-positive rate | evidence accuracy | severity accuracy | cross-file recall | matched / expected | findings |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| scanners_only | 3 | 0.667 | 1.000 | 0.000 | 1.000 | 1.000 | 0.000 | 12 / 18 | 25 |
| single_agent_no_tools | 3 | 0.685 | 0.925 | 0.075 | 1.000 | 1.000 | 0.056 | 12 / 18 | 32 |
| multi_agent_validated | 3 | 0.944 | 1.000 | 0.000 | 0.941 | 0.924 | 0.833 | 17 / 18 | 30 |

## Spread across the three runs

Minimum – maximum of the same per-run totals. A zero-width range means the three runs produced the same value.

| configuration | metric | run 1 | run 2 | run 3 | range |
| --- | --- | ---: | ---: | ---: | ---: |
| scanners_only | recall | 0.667 | 0.667 | 0.667 | 0.667 – 0.667 |
| scanners_only | precision | 1.000 | 1.000 | 1.000 | 1.000 – 1.000 |
| scanners_only | false-positive rate | 0.000 | 0.000 | 0.000 | 0.000 – 0.000 |
| scanners_only | evidence accuracy | 1.000 | 1.000 | 1.000 | 1.000 – 1.000 |
| scanners_only | severity accuracy | 1.000 | 1.000 | 1.000 | 1.000 – 1.000 |
| scanners_only | cross-file recall | 0.000 | 0.000 | 0.000 | 0.000 – 0.000 |
| scanners_only | matched issues | 12 | 12 | 12 | 12 – 12 |
| scanners_only | findings reported | 25 | 25 | 25 | 25 – 25 |
| scanners_only | duplicate findings | 13 | 13 | 13 | 13 – 13 |
| scanners_only | false positives | 0 | 0 | 0 | 0 – 0 |
| single_agent_no_tools | recall | 0.667 | 0.667 | 0.722 | 0.667 – 0.722 |
| single_agent_no_tools | precision | 0.923 | 0.923 | 0.929 | 0.923 – 0.929 |
| single_agent_no_tools | false-positive rate | 0.077 | 0.077 | 0.071 | 0.071 – 0.077 |
| single_agent_no_tools | evidence accuracy | 1.000 | 1.000 | 1.000 | 1.000 – 1.000 |
| single_agent_no_tools | severity accuracy | 1.000 | 1.000 | 1.000 | 1.000 – 1.000 |
| single_agent_no_tools | cross-file recall | 0.000 | 0.000 | 0.167 | 0.000 – 0.167 |
| single_agent_no_tools | matched issues | 12 | 12 | 13 | 12 – 13 |
| single_agent_no_tools | findings reported | 31 | 32 | 33 | 31 – 33 |
| single_agent_no_tools | duplicate findings | 18 | 19 | 19 | 18 – 19 |
| single_agent_no_tools | false positives | 1 | 1 | 1 | 1 – 1 |
| multi_agent_validated | recall | 0.889 | 0.944 | 1.000 | 0.889 – 1.000 |
| multi_agent_validated | precision | 1.000 | 1.000 | 1.000 | 1.000 – 1.000 |
| multi_agent_validated | false-positive rate | 0.000 | 0.000 | 0.000 | 0.000 – 0.000 |
| multi_agent_validated | evidence accuracy | 0.938 | 0.941 | 0.944 | 0.938 – 0.944 |
| multi_agent_validated | severity accuracy | 1.000 | 0.882 | 0.889 | 0.882 – 1.000 |
| multi_agent_validated | cross-file recall | 0.667 | 0.833 | 1.000 | 0.667 – 1.000 |
| multi_agent_validated | matched issues | 16 | 17 | 18 | 16 – 18 |
| multi_agent_validated | findings reported | 29 | 30 | 31 | 29 – 31 |
| multi_agent_validated | duplicate findings | 13 | 13 | 13 | 13 – 13 |
| multi_agent_validated | false positives | 0 | 0 | 0 | 0 – 0 |

## Cost and runtime

Cost is the sum of stored `agent_runs` spend for the run, over the four repositories. Runtime is the harness wall clock for the run, indexing included.

| configuration | cost per run (USD) | mean cost | runtime per run (s) | mean runtime |
| --- | --- | ---: | --- | ---: |
| scanners_only | 0.0000, 0.0000, 0.0000 | 0.0000 | 67.7, 62.6, 64.8 | 65.0 |
| single_agent_no_tools | 0.0026, 0.0028, 0.0027 | 0.0027 | 290.4, 166.3, 182.4 | 213.0 |
| multi_agent_validated | 0.0213, 0.0173, 0.0169 | 0.0185 | 783.4, 423.6, 1105.4 | 770.8 |

## Planted issues missed

Per configuration, the planted issue identifiers absent from the matched set. An issue listed under *missed in all three runs* was missed every time; one listed with a count was missed in that many runs.

### scanners_only

Missed in all three runs (6 of 6): `adv-delete-authz-02`, `adv-export-authz-01`, `js-authz-01`, `js-cycle-01`, `py-authz-01`, `py-cycle-01`.

Per repository, per run:

| run | repository | missed issues |
| --- | --- | --- |
| stage2-scanners_only-1 | adversarial_app | `adv-export-authz-01`, `adv-delete-authz-02` |
| stage2-scanners_only-1 | js_app | `js-authz-01`, `js-cycle-01` |
| stage2-scanners_only-1 | python_app | `py-authz-01`, `py-cycle-01` |
| stage2-scanners_only-2 | adversarial_app | `adv-export-authz-01`, `adv-delete-authz-02` |
| stage2-scanners_only-2 | js_app | `js-authz-01`, `js-cycle-01` |
| stage2-scanners_only-2 | python_app | `py-authz-01`, `py-cycle-01` |
| stage2-scanners_only-3 | adversarial_app | `adv-export-authz-01`, `adv-delete-authz-02` |
| stage2-scanners_only-3 | js_app | `js-authz-01`, `js-cycle-01` |
| stage2-scanners_only-3 | python_app | `py-authz-01`, `py-cycle-01` |

### single_agent_no_tools

Missed in all three runs (5 of 6): `adv-delete-authz-02`, `adv-export-authz-01`, `js-cycle-01`, `py-authz-01`, `py-cycle-01`.

Missed in some runs: `js-authz-01` (2/3).

Per repository, per run:

| run | repository | missed issues |
| --- | --- | --- |
| stage2-single_agent_no_tools-1 | adversarial_app | `adv-export-authz-01`, `adv-delete-authz-02` |
| stage2-single_agent_no_tools-1 | js_app | `js-authz-01`, `js-cycle-01` |
| stage2-single_agent_no_tools-1 | python_app | `py-authz-01`, `py-cycle-01` |
| stage2-single_agent_no_tools-2 | adversarial_app | `adv-export-authz-01`, `adv-delete-authz-02` |
| stage2-single_agent_no_tools-2 | js_app | `js-authz-01`, `js-cycle-01` |
| stage2-single_agent_no_tools-2 | python_app | `py-authz-01`, `py-cycle-01` |
| stage2-single_agent_no_tools-3 | adversarial_app | `adv-export-authz-01`, `adv-delete-authz-02` |
| stage2-single_agent_no_tools-3 | js_app | `js-cycle-01` |
| stage2-single_agent_no_tools-3 | python_app | `py-authz-01`, `py-cycle-01` |

### multi_agent_validated

Missed in all three runs: none.

Missed in some runs: `adv-delete-authz-02` (2/3), `js-authz-01` (1/3).

Per repository, per run:

| run | repository | missed issues |
| --- | --- | --- |
| stage2-multi_agent_validated-1 | adversarial_app | `adv-delete-authz-02` |
| stage2-multi_agent_validated-1 | js_app | `js-authz-01` |
| stage2-multi_agent_validated-2 | adversarial_app | `adv-delete-authz-02` |

## Runs

| label | configuration | stored run id | started (UTC) |
| --- | --- | --- | --- |
| stage2-scanners_only-1 | scanners_only | `bd7084b8-4c55-48be-a2d6-5654c70f0c7b` | 2026-11-13 18:34:26 |
| stage2-scanners_only-2 | scanners_only | `d310bbf1-6bb0-4cab-86b7-e264057c4c1f` | 2026-11-13 18:35:36 |
| stage2-scanners_only-3 | scanners_only | `ca76a028-0ed5-444a-8542-6af9838f6118` | 2026-11-13 18:36:41 |
| stage2-single_agent_no_tools-1 | single_agent_no_tools | `d115689d-7cfa-44b3-85d4-e641fe544746` | 2026-11-13 18:37:48 |
| stage2-single_agent_no_tools-2 | single_agent_no_tools | `a4f00630-c831-40f1-83eb-4722835dffc8` | 2026-11-13 18:42:40 |
| stage2-single_agent_no_tools-3 | single_agent_no_tools | `261cbb11-63b8-4f81-83d5-6b43743b6494` | 2026-11-13 18:45:28 |
| stage2-multi_agent_validated-1 | multi_agent_validated | `2076723e-6b4f-47e4-92b2-a68f2033ba6a` | 2026-11-13 18:48:32 |
| stage2-multi_agent_validated-2 | multi_agent_validated | `f1350801-0ec4-4135-af71-46fa36d33123` | 2026-11-13 19:01:38 |
| stage2-multi_agent_validated-3 | multi_agent_validated | `fc8d9df3-63d4-408c-9bed-d0090225a128` | 2026-11-13 19:08:44 |
