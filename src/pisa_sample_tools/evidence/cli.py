from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pisa_sample_tools.outcome_eval.cli import main as outcome_eval_main
from pisa_sample_tools.sample_export.cli import main as sample_export_main
from pisa_sample_tools.sampler_preview.cli import main as sample_preview_main
from pisa_sample_tools.trajectory.cli import main as trajectory_main
from pisa_sample_tools.trajectory_compare.cli import main as trajectory_compare_main

from .models import EvidenceError
from .service import build_evidence, enrich_sensitivity_bundle, validate_evidence_inputs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pisa-analysis",
        description="Build reproducible validation evidence from PISA runner results.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "report", "compare", "validate"):
        command = subparsers.add_parser(name)
        source = command.add_mutually_exclusive_group(required=True)
        source.add_argument(
            "--results",
            type=Path,
            action="append",
            help="Runner result root. Repeat for component or repeated-run comparison.",
        )
        source.add_argument(
            "--campaign",
            type=Path,
            help="Analysis-side campaign YAML containing result roots and comparison labels.",
        )
        command.add_argument("--spec", type=Path, help="Versioned analysis_spec.yaml.")
        command.add_argument(
            "--validation", choices=("strict", "permissive"), help="Override spec validation mode."
        )
        if name == "validate":
            command.add_argument(
                "--summary-only", action="store_true", help="Skip expensive trace alignment checks."
            )
        else:
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--overwrite", action="store_true")
            command.add_argument(
                "--profile", action="store_true", help="Write stage timings (enabled for all builds)."
            )
            command.add_argument(
                "--report-mode",
                choices=("interactive", "static"),
                default="interactive",
                help="HTML report mode. Interactive is the default offline dashboard.",
            )
            command.add_argument(
                "--sensitivity",
                action=argparse.BooleanOptionalAction,
                default=None,
                help="Run parameter sensitivity training. Disabled by default unless enabled in the spec.",
            )
    sensitivity = subparsers.add_parser(
        "sensitivity", help="Compute sensitivity for an existing evidence bundle."
    )
    sensitivity.add_argument("--bundle", type=Path, required=True)
    subparsers.add_parser("trajectory", add_help=False)
    builder = subparsers.add_parser("builder", help="Launch the interactive report builder.")
    builder.add_argument("--host", default="127.0.0.1")
    builder.add_argument("--port", type=int, default=0)
    builder.add_argument("--no-open", action="store_true")
    subparsers.add_parser("trajectory-compare", add_help=False)
    subparsers.add_parser("outcome-eval", add_help=False)
    sample = subparsers.add_parser("sample")
    sample_subparsers = sample.add_subparsers(dest="sample_command", required=True)
    sample_subparsers.add_parser("preview", add_help=False)
    sample_subparsers.add_parser("export", add_help=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["trajectory"]:
        return trajectory_main(argv[1:])
    if argv[:1] == ["trajectory-compare"]:
        return trajectory_compare_main(argv[1:])
    if argv[:1] == ["outcome-eval"]:
        return outcome_eval_main(argv[1:])
    if argv[:2] == ["sample", "preview"]:
        return sample_preview_main(argv[2:])
    if argv[:2] == ["sample", "export"]:
        return sample_export_main(argv[2:])
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "builder":
        from .builder_server import run_builder

        try:
            run_builder(host=args.host, port=args.port, open_browser=not args.no_open)
        except EvidenceError as exc:
            parser.error(str(exc))
        return 0
    if args.command == "sensitivity":
        try:
            result = enrich_sensitivity_bundle(
                args.bundle,
                progress=lambda message: print(message, file=sys.stderr),
            )
        except EvidenceError as exc:
            parser.error(str(exc))
        print(f"sensitivity targets: {len(result.model_quality)}")
        print(f"bundle: {args.bundle.expanduser().resolve()}")
        return 0
    if args.command == "validate":
        try:
            run_count, findings = validate_evidence_inputs(
                results_paths=args.results,
                campaign_path=args.campaign,
                spec_path=args.spec,
                validation_mode=args.validation,
                deep=not args.summary_only,
                progress=lambda message: print(message, file=sys.stderr),
            )
        except EvidenceError as exc:
            parser.error(str(exc))
        for finding in findings:
            location = f" [{finding.run_id}]" if finding.run_id else ""
            print(f"{finding.severity.upper()} {finding.code}{location}: {finding.message}")
        errors = sum(item.severity == "error" for item in findings)
        warnings = sum(item.severity == "warning" for item in findings)
        print(f"runs: {run_count}")
        print(f"errors: {errors}")
        print(f"warnings: {warnings}")
        return 1 if errors else 0
    try:
        result = build_evidence(
            results_paths=args.results,
            campaign_path=args.campaign,
            output_dir=args.output,
            spec_path=args.spec,
            overwrite=args.overwrite,
            progress=lambda message: print(message, file=sys.stderr),
            validation_mode=args.validation,
            report_mode=args.report_mode,
            sensitivity=args.sensitivity,
        )
    except EvidenceError as exc:
        parser.error(str(exc))
    print(f"runs: {result.run_count}")
    print(f"warnings: {result.warning_count}")
    print(f"report: {result.report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
