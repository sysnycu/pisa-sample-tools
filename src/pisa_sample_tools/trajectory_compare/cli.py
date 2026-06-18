from __future__ import annotations

import argparse
from pathlib import Path

from .models import TrajectoryCompareError
from .service import compare_trajectory_sets


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pisa-trajectory-compare",
        description="Compare non-ego agent trajectories from two simulator result sets.",
    )
    parser.add_argument(
        "--left",
        type=Path,
        required=True,
        help="Left result dir, iteration dir, or agent_states.csv.",
    )
    parser.add_argument(
        "--right",
        type=Path,
        required=True,
        help="Right result dir, iteration dir, or agent_states.csv.",
    )
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="Directory for comparison SVGs and metrics."
    )
    parser.add_argument("--left-label", help="Display label for the left simulator/result set.")
    parser.add_argument("--right-label", help="Display label for the right simulator/result set.")
    parser.add_argument(
        "--ignore-agent-id",
        action="append",
        default=["0"],
        help="Agent id to ignore. Defaults to 0. Can be repeated.",
    )
    parser.add_argument("--width", type=int, default=1200, help="SVG width in pixels.")
    parser.add_argument("--height", type=int, default=820, help="SVG height in pixels.")
    parser.add_argument(
        "--scale-mode",
        choices=["equal", "stretch"],
        default="equal",
        help="Use equal x/y scale or stretch x/y independently to fill the plot area.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite a previous tool output directory containing manifest.yaml.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = compare_trajectory_sets(
            left_path=args.left,
            right_path=args.right,
            output_dir=args.output_dir,
            left_label=args.left_label,
            right_label=args.right_label,
            ignore_agent_ids=set(args.ignore_agent_id),
            overwrite=args.overwrite,
            width=args.width,
            height=args.height,
            equal_scale=args.scale_mode == "equal",
        )
    except (TrajectoryCompareError, ValueError) as exc:
        parser.error(str(exc))

    print(f"comparison_count: {len(result.comparisons)}")
    print(f"summary_csv: {result.summary_csv_path}")
    print(f"manifest: {result.manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
