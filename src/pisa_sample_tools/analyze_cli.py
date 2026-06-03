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
    source.add_argument("--samples", type=Path, help="explicit.yaml, bundle dir, or bundle output root.")
    source.add_argument("--results", type=Path, help="Runner output directory containing iteration_* results.")
    parser.add_argument(
        "--params",
        help="Comma-separated parameter names to plot. Omit to auto-select up to 3 numeric params.",
    )
    parser.add_argument(
        "--color-by",
        default="outcome",
        help="Color points by none, outcome, status, stop_condition, param:<name>, or metric:<name>.",
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
            overwrite=args.overwrite,
        )
    except (AnalyzeError, ValueError) as exc:
        parser.error(str(exc))

    print(f"records: {result.record_count}")
    print(f"params: {', '.join(result.selected_params)}")
    print(f"report: {result.report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
