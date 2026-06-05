from __future__ import annotations

import argparse
from pathlib import Path

from .models import TrajectoryError
from .service import visualize_trajectories


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pisa-sample-trajectory",
        description="Render agent trajectory SVGs from runner agent_states.csv files.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="agent_states.csv, one iteration directory, or a runner results folder.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory for generated trajectory SVG files.",
    )
    parser.add_argument("--width", type=int, default=1100, help="SVG width in pixels.")
    parser.add_argument("--height", type=int, default=760, help="SVG height in pixels.")
    parser.add_argument(
        "--x-range",
        help="Only draw points with x inside min,max. Example: --x-range -20,80",
    )
    parser.add_argument(
        "--y-range",
        help="Only draw points with y inside min,max. Example: --y-range -10,30",
    )
    parser.add_argument(
        "--scale-mode",
        choices=["equal", "stretch"],
        default="equal",
        help="Use equal x/y scale or stretch x/y independently to fill the plot area.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite a previous trajectory output directory containing manifest.yaml.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        x_range = _parse_range(args.x_range, label="x-range")
        y_range = _parse_range(args.y_range, label="y-range")
        result = visualize_trajectories(
            input_path=args.input,
            output_dir=args.output_dir,
            overwrite=args.overwrite,
            width=args.width,
            height=args.height,
            x_range=x_range,
            y_range=y_range,
            equal_scale=args.scale_mode == "equal",
        )
    except TrajectoryError as exc:
        parser.error(str(exc))

    print(f"svg_count: {len(result.results)}")
    print(f"output_dir: {result.output_dir}")
    print(f"manifest: {result.manifest_path}")
    return 0


def _parse_range(value: str | None, *, label: str) -> tuple[float, float] | None:
    if value is None:
        return None
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 2 or not all(parts):
        raise TrajectoryError(f"{label} must be formatted as min,max")
    try:
        return float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise TrajectoryError(f"{label} values must be numeric") from exc


if __name__ == "__main__":
    raise SystemExit(main())

