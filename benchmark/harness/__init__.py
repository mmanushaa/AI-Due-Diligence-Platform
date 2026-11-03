"""Evaluation harness for the audit pipeline (step 2.6).

Runs one configuration over every fixture repository and scores the findings
against ``benchmark/expected.json``. The pieces are deliberately separable so the
arithmetic can be tested without a database, Docker or a model:

    expected.py   the manifest: what should be found, and where
    configs.py    the four configurations and whether each can run today
    matching.py   the matching rule (same file, overlapping lines, same category)
    evidence.py   "do the cited lines contain the claimed thing?"
    metrics.py    per-repo and aggregate numbers, kept with their denominators
    runner.py     import a fixture, run a configuration, write eval_runs/eval_results
    report.py     the table and the arithmetic behind it

Nothing here trusts a detector's own claim: a finding only counts when the
manifest agrees about the file, the lines and the category.
"""
