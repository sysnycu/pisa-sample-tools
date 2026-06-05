from __future__ import annotations

import argparse
from pathlib import Path

from .service import OutcomeEvalError, OutcomeEvalMode, evaluate_outcomes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pisa-outcome-eval",
        description="Evaluate offline outcome conditions against completed runner monitor logs.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="One iteration directory or a runner results folder containing iteration_* directories.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="YAML condition tree config for offline outcome evaluation.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory for offline_outcomes.csv and manifest.yaml.",
    )
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in OutcomeEvalMode],
        default=OutcomeEvalMode.REPLACE.value,
        help=(
            "replace: fully re-evaluate outcome and use --default-outcome when no condition triggers. "
            "overlay: keep original outcome unless a condition triggers."
        ),
    )
    parser.add_argument(
        "--default-outcome",
        default="unknown",
        help="Outcome when no offline condition triggers in replace mode. Defaults to unknown.",
    )
    parser.add_argument(
        "--write-monitor-outcome",
        action="store_true",
        help="Also write monitor/offline_outcome.csv next to each evaluated result.csv.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite a previous outcome-eval output directory containing manifest.yaml.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = evaluate_outcomes(
            input_path=args.input,
            config_path=args.config,
            output_dir=args.output_dir,
            mode=OutcomeEvalMode(args.mode),
            default_outcome=args.default_outcome,
            overwrite=args.overwrite,
            write_monitor_outcome=args.write_monitor_outcome,
        )
    except OutcomeEvalError as exc:
        parser.error(str(exc))

    triggered = sum(outcome.triggered for outcome in result.outcomes)
    print(f"scenario_count: {len(result.outcomes)}")
    print(f"triggered_count: {triggered}")
    print(f"summary_csv: {result.summary_csv_path}")
    print(f"manifest: {result.manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

