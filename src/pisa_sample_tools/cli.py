from __future__ import annotations

import argparse
from pathlib import Path

from pisa_sample_tools.exporter import ExportError, SourcePathMode, export_samples


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pisa-sample-export",
        description="Generate explicit sample shards from a PISA runner sampler spec.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--runner-spec", type=Path, help="Path to runner spec JSON/YAML file.")
    source.add_argument(
        "--sampler-spec",
        type=Path,
        help="Path to a standalone sampler runtime spec JSON/YAML file.",
    )
    parser.add_argument(
        "--scenario-path",
        type=Path,
        help="Scenario path used as source base when --sampler-spec is provided.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Output directory. Defaults to output/{scenario_name}-{sampler_name}-{total_samples}.",
    )
    split = parser.add_mutually_exclusive_group(required=True)
    split.add_argument("--shard-size", type=int, help="Maximum samples per shard.")
    split.add_argument("--num-shards", type=int, help="Number of shards to create.")
    parser.add_argument(
        "--source-path-mode",
        choices=[mode.value for mode in SourcePathMode],
        default=SourcePathMode.ABSOLUTE.value,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow writing to an existing empty output directory.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        export_samples(
            output_dir=args.output_dir,
            runner_spec_path=args.runner_spec,
            sampler_spec_path=args.sampler_spec,
            scenario_path=args.scenario_path,
            shard_size=args.shard_size,
            num_shards=args.num_shards,
            source_path_mode=SourcePathMode(args.source_path_mode),
            overwrite=args.overwrite,
        )
    except ExportError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
