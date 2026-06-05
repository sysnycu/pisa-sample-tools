from __future__ import annotations

import argparse
import json
from pathlib import Path

from .models import ExportError, SourcePathMode
from .service import export_samples


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
    parser.add_argument(
        "--zip",
        dest="create_zip",
        action="store_true",
        help="Create a zip archive for the generated bundles, excluding manifest.yaml.",
    )
    parser.add_argument(
        "--zip-path",
        type=Path,
        help="Optional archive path. Implies --zip when provided.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve inputs and compute bundles without writing output files.",
    )
    parser.add_argument(
        "--summary",
        nargs="?",
        const="text",
        choices=["text", "json"],
        help="Print an export summary. Use '--summary json' for machine-readable output.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        result = export_samples(
            output_dir=args.output_dir,
            runner_spec_path=args.runner_spec,
            sampler_spec_path=args.sampler_spec,
            scenario_path=args.scenario_path,
            shard_size=args.shard_size,
            num_shards=args.num_shards,
            source_path_mode=SourcePathMode(args.source_path_mode),
            create_zip=args.create_zip or args.zip_path is not None,
            zip_path=args.zip_path,
            dry_run=args.dry_run,
            overwrite=args.overwrite,
        )
    except ExportError as exc:
        parser.error(str(exc))
    summary_mode = args.summary or ("text" if args.dry_run else None)
    if summary_mode == "json":
        print(json.dumps(result.summary, indent=2))
    elif summary_mode == "text":
        assert result.summary is not None
        print(format_summary(result.summary))
    return 0


def format_summary(summary: dict[str, object]) -> str:
    lines = [
        f"dry_run: {summary['dry_run']}",
        f"scenario: {summary['scenario_name']}",
        f"sampler: {summary['sampler_name']}",
        f"source: {summary['source_path']} ({summary['source_type']})",
        f"total_samples: {summary['total_samples']}",
        f"shard_count: {summary['shard_count']}",
        f"output_dir: {summary['output_dir']}",
        f"zip_path: {summary['zip_path']}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())

