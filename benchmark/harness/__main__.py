"""Command line entry point: ``python -m benchmark.harness --config scanners_only``.

Options:

    --config NAME     scanners_only (default), single_agent, multi_agent,
                      multi_agent_validated
    --repo NAME       limit to one fixture repository; repeatable
    --list            show every configuration and whether it can run here
    --no-save         run and print without writing eval_runs/eval_results
    --keep-audits     keep the throwaway audits instead of deleting them
    --manifest PATH   a different expected.json

Exit status: 0 when the run completed, 2 when the requested configuration cannot
run in this checkout, 1 on any other failure.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from benchmark.harness import bootstrap

bootstrap.add_backend_to_path()

from benchmark.harness import configs, report, runner  # noqa: E402
from benchmark.harness.expected import ManifestError, load_manifest  # noqa: E402

HERE = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = HERE / "expected.json"
DEFAULT_REPOS = HERE / "repos"


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m benchmark.harness",
        description="Score a pipeline configuration against benchmark/expected.json.",
    )
    parser.add_argument("--config", default="scanners_only", help="configuration to evaluate")
    parser.add_argument("--repo", action="append", dest="repos", help="fixture repository (repeatable)")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="expected.json to score against")
    parser.add_argument("--list", action="store_true", help="list configurations and exit")
    parser.add_argument("--no-save", action="store_true", help="do not write eval_runs/eval_results")
    parser.add_argument("--keep-audits", action="store_true", help="keep the throwaway audits")
    return parser.parse_args(argv)


def _print_configurations() -> None:
    for configuration in configs.CONFIGURATIONS:
        available, reason = configs.availability(configuration)
        mark = "run" if available else "n/a"
        print(f"  [{mark}] {configuration.name:<22} {configuration.summary}")
        if reason:
            print(f"        {reason}")


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)

    if args.list:
        _print_configurations()
        return 0

    try:
        configuration = configs.get(args.config)
    except configs.ConfigurationUnavailable as exc:
        print(f"{exc}\n", file=sys.stderr)
        _print_configurations()
        return 2

    available, reason = configs.availability(configuration)
    if not available:
        print(f"cannot run {configuration.name}: {reason}", file=sys.stderr)
        print("\nconfigurations in this checkout:", file=sys.stderr)
        _print_configurations()
        return 2

    try:
        manifest = load_manifest(Path(args.manifest), DEFAULT_REPOS)
    except ManifestError as exc:
        print(f"manifest error: {exc}", file=sys.stderr)
        return 1

    unknown = [repo for repo in (args.repos or ()) if repo not in manifest.repos]
    if unknown:
        print(f"unknown repository: {', '.join(unknown)}; have {', '.join(manifest.repos)}", file=sys.stderr)
        return 1

    record = runner.run_configuration(
        configuration,
        manifest,
        repos=tuple(args.repos) if args.repos else None,
        repos_root=DEFAULT_REPOS,
        keep_audits=args.keep_audits,
        persist=not args.no_save,
    )
    print(report.render(record))
    if not args.no_save:
        print(f"\nsaved: eval_runs {record.eval_run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
