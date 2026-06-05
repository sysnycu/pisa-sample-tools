from __future__ import annotations

import argparse
from pathlib import Path

from pisa_sample_tools.analyze import AnalyzeError, analyze_samples


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pisa-sample-analyze",
        description="Analyze PISA sample distributions and runner result folders.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--runner-spec", type=Path, help="Runner spec JSON/YAML to materialize.")
    source.add_argument(
        "--samples",
        type=Path,
        help="explicit_samples.yaml, legacy explicit.yaml, bundle dir, or bundle output root.",
    )
    source.add_argument("--results", type=Path, help="Runner output directory containing iteration_* results.")
    parser.add_argument(
        "--params",
        help=(
            "Optional comma-separated initial X/Y/Z parameter names. "
            "All discovered params remain selectable in the report."
        ),
    )
    parser.add_argument(
        "--color-by",
        default="outcome",
        help="Color points by none, outcome, status, stop_condition, param:<name>, or metric:<name>.",
    )
    parser.add_argument(
        "--bins",
        type=int,
        default=28,
        help="Default 1D histogram bin count for static figures and report.html.",
    )
    parser.add_argument(
        "--post-outcome-config",
        type=Path,
        help="Optional offline outcome condition YAML to evaluate after loading --results.",
    )
    parser.add_argument(
        "--post-outcome-mode",
        choices=["overlay", "replace"],
        default="overlay",
        help=(
            "overlay keeps original outcomes unless post condition triggers; "
            "replace uses the post condition tree as the full outcome definition."
        ),
    )
    parser.add_argument("--output", type=Path, required=True, help="Analysis output directory.")
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing analysis output.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    params = args.params.split(",") if args.params else None
    try:
        result = analyze_samples(
            output_dir=args.output,
            runner_spec_path=args.runner_spec,
            samples_path=args.samples,
            results_path=args.results,
            params=params,
            color_by=args.color_by,
            bins=args.bins,
            post_outcome_config_path=args.post_outcome_config,
            post_outcome_mode=args.post_outcome_mode,
            overwrite=args.overwrite,
        )
    except (AnalyzeError, ValueError) as exc:
        parser.error(str(exc))

    print(f"records: {result.record_count}")
    print(f"initial_params: {', '.join(result.selected_params)}")
    print(f"report: {result.report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
