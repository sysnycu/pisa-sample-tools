from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Header, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.exceptions import HTTPException as StarletteHTTPException

from .errors import APIError
from .jobs import TERMINAL_STATES, JobContext, JobManager
from .models import (
    ConfirmationRequest,
    ConsistencyAnalyzeRequest,
    DirectoryCreateRequest,
    ErrorBody,
    ExportRequest,
    Job,
    LegacyRebuildRequest,
    MediaCreateRequest,
    OutcomeEvalRequest,
    PairedMetricAgreementRequest,
    PairedParameterAnalysisRequest,
    RepairApplyRequest,
    RepairRestoreRequest,
    RepairScanRequest,
    ReportBuildRequest,
    ReportDeleteRequest,
    ReportPersistRequest,
    ReportPreviewBuildRequest,
    ReportRenameRequest,
    ReportValidateRequest,
    RunnerCleanupRequest,
    RunnerExperimentRequest,
    RunnerJobRequest,
    RunnerPresetCreateRequest,
    RunnerPresetRenameRequest,
    RunnerPresetUpdateRequest,
    RunnerRegistryRequest,
    RunnerResumeRequest,
    RunnerScenarioRequest,
    SampleAnalyzeRequest,
    SampleExportRequest,
    SamplerPreviewRequest,
    SnapshotRequest,
    TrajectoryCompareRequest,
    TrajectoryRenderRequest,
    TrajectoryRequest,
)
from .paths import PathPolicy
from .repair import RepairService
from .reports import ReportLibrary, ensure_report_index, report_id

API_PREFIX = "/api/v1"
API_VERSION = "1"


def create_app(
    *,
    report_roots: Sequence[Path] | None = None,
    results_roots: Sequence[Path] | None = None,
    config: Path | None = None,
    local_config: Path | None = None,
    state_path: Path | None = None,
    frontend_dir: Path | None = None,
    job_manager: JobManager | None = None,
    runner_store: Any | None = None,
    runner_manager: Any | None = None,
) -> FastAPI:
    report_roots = _normalize_roots(report_roots)
    results_roots = _normalize_roots(results_roots)
    roots = _deduplicate([*report_roots, *results_roots, Path.cwd().resolve()])
    policy = PathPolicy(roots)
    preview_root = Path(state_path).expanduser().resolve().parent / ".report-previews" if state_path is not None else None
    reports = ReportLibrary(report_roots or roots, policy, temporary_root=preview_root)
    repairs = RepairService(policy)
    jobs = job_manager or JobManager(state_path)
    runner_store, runner_manager = _runner_services(
        config=config,
        local_config=local_config,
        store=runner_store,
        manager=runner_manager,
    )
    selected_frontend = _find_frontend(frontend_dir)

    app = FastAPI(
        title="PISA Analysis Workbench API",
        version=API_VERSION,
        description="Unified, local-first API for PISA sampling, experiments, reports, and tools.",
    )
    app.state.path_policy = policy
    app.state.report_library = reports
    app.state.job_manager = jobs
    app.state.repair_service = repairs
    app.state.runner_store = runner_store
    app.state.runner_manager = runner_manager
    app.state.frontend_dir = selected_frontend
    app.router.add_event_handler("shutdown", reports.close)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next: Callable[..., Any]):
        import uuid

        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(APIError)
    async def api_error(request: Request, exc: APIError) -> JSONResponse:
        return _error_response(
            request,
            exc.status_code,
            exc.code,
            exc.message,
            field=exc.field,
            details=exc.details,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        first = errors[0] if errors else {}
        location = [str(item) for item in first.get("loc", ()) if item not in {"body", "query"}]
        return _error_response(
            request,
            422,
            "validation_error",
            str(first.get("msg") or "request validation failed"),
            field=".".join(location) or None,
            details={"errors": jsonable_encoder(errors)},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        message = str(detail) if not isinstance(detail, dict) else str(detail.get("message") or detail)
        return _error_response(request, exc.status_code, f"http_{exc.status_code}", message)

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception) -> JSONResponse:
        return _error_response(
            request,
            500,
            "internal_error",
            "an unexpected server error occurred",
            details={"exception": type(exc).__name__},
        )

    @app.get(f"{API_PREFIX}/health", tags=["system"])
    async def health() -> dict[str, Any]:
        return {"status": "ok", "api_version": API_VERSION, "schema_version": 1}

    @app.get(f"{API_PREFIX}/capabilities", tags=["system"])
    async def capabilities() -> dict[str, Any]:
        return _capabilities(
            frontend=selected_frontend,
            runner_store=runner_store,
            roots=policy.roots,
        )

    @app.post(f"{API_PREFIX}/samples/preview", tags=["samples"])
    async def sample_preview(payload: SamplerPreviewRequest) -> dict[str, Any]:
        if payload.source_file is None:
            from .inline_sampling import InlineSamplerError, generate_inline_samples

            try:
                return generate_inline_samples(
                    method=payload.method or "",
                    count=payload.count or 0,
                    seed=payload.seed,
                    parameters=payload.parameters or [],
                )
            except InlineSamplerError as exc:
                raise APIError(400, "inline_sampler_invalid", str(exc)) from exc

        from pisa_sample_tools.sampler_preview import collect_sampler_preview

        source = policy.resolve(payload.source_file, field="source_file", kind="file")
        config_path = (
            policy.resolve(payload.config_path, field="config_path", kind="file")
            if payload.config_path
            else None
        )
        try:
            result = collect_sampler_preview(
                source_file=source,
                sampler_name=payload.sampler_name,
                source_type=payload.source_type,
                module_path=payload.module_path,
                config_path=config_path,
                config=payload.config,
                max_samples=payload.max_samples,
            )
        except (OSError, ValueError) as exc:
            raise APIError(400, "sampler_preview_failed", str(exc)) from exc
        return jsonable_encoder(result)

    @app.post(
        f"{API_PREFIX}/samples/export", response_model=Job, status_code=202, tags=["samples"]
    )
    async def sample_export(payload: SampleExportRequest) -> Job:
        output = policy.resolve(
            payload.output_dir, field="output_dir", must_exist=False, kind="directory"
        )
        runner_spec = _optional_file(policy, payload.runner_spec_path, "runner_spec_path")
        sampler_spec = _optional_file(policy, payload.sampler_spec_path, "sampler_spec_path")
        scenario = _optional_path(policy, payload.scenario_path, "scenario_path")
        zip_path = (
            policy.resolve(payload.zip_path, field="zip_path", must_exist=False)
            if payload.zip_path
            else None
        )

        def run(context: JobContext) -> Any:
            from pisa_sample_tools.sample_export import SourcePathMode, export_samples

            context.progress("generating_samples", message="Generating and sharding samples")
            result = export_samples(
                output_dir=output,
                runner_spec_path=runner_spec,
                sampler_spec_path=sampler_spec,
                scenario_path=scenario,
                shard_size=payload.shard_size,
                num_shards=payload.num_shards,
                source_path_mode=SourcePathMode(payload.source_path_mode),
                create_zip=payload.create_zip,
                zip_path=zip_path,
                dry_run=payload.dry_run,
                overwrite=payload.overwrite,
                include_map=payload.include_map,
                map_reference=payload.map_reference,
                map_boundaries=payload.map_boundaries,
                map_junctions=payload.map_junctions,
                show_bounding_boxes=payload.show_bounding_boxes,
                follow_cursor=payload.follow_cursor,
                trail_only=payload.trail_only,
                render_mode=payload.render_mode,
                show_ego=payload.show_ego,
                show_agents=payload.show_agents,
                actor_names=payload.actor_names,
                show_goal=payload.show_goal,
                show_grid=payload.show_grid,
                show_axes=payload.show_axes,
                x_range=(payload.x_min, payload.x_max) if payload.x_min is not None else None,
                y_range=(payload.y_min, payload.y_max) if payload.y_min is not None else None,
            )
            if not payload.dry_run:
                context.artifact(result.manifest_path, kind="manifest")
                if result.zip_path:
                    context.artifact(result.zip_path, kind="archive")
            return result

        return jobs.submit("sample_export", payload.model_dump(), run)

    @app.post(
        f"{API_PREFIX}/samples/analyze", response_model=Job, status_code=202, tags=["samples"]
    )
    async def sample_analyze(payload: SampleAnalyzeRequest) -> Job:
        output = policy.resolve(
            payload.output_dir, field="output_dir", must_exist=False, kind="directory"
        )
        runner_spec = _optional_file(policy, payload.runner_spec_path, "runner_spec_path")
        samples = _optional_path(policy, payload.samples_path, "samples_path")
        results = _optional_path(policy, payload.results_path, "results_path")
        outcome_config = _optional_file(
            policy, payload.post_outcome_config_path, "post_outcome_config_path"
        )

        def run(context: JobContext) -> Any:
            from pisa_sample_tools.sample_analyze import analyze_samples

            context.progress("loading_samples", message="Loading sample records")
            result = analyze_samples(
                output_dir=output,
                runner_spec_path=runner_spec,
                samples_path=samples,
                results_path=results,
                params=payload.params,
                color_by=payload.color_by,
                bins=payload.bins,
                post_outcome_config_path=outcome_config,
                post_outcome_mode=payload.post_outcome_mode,
                overwrite=payload.overwrite,
            )
            context.artifact(result.report_path, kind="report")
            context.artifact(result.csv_path, kind="data")
            return result

        return jobs.submit("sample_analyze", payload.model_dump(), run)

    @app.get(f"{API_PREFIX}/reports", tags=["reports"])
    async def report_catalog(
        root: str | None = None,
        search: str | None = Query(default=None, max_length=200),
        recursive: bool = True,
    ) -> dict[str, Any]:
        result = reports.scan(root, recursive=recursive)
        if search:
            needle = search.casefold()
            result = {
                **result,
                "reports": [
                    item
                    for item in result["reports"]
                    if needle
                    in f"{item.get('name', '')} {item.get('path', '')}".casefold()
                ],
            }
        return {**result, "items": result["reports"]}

    @app.get(f"{API_PREFIX}/reports/browser", tags=["reports"])
    async def report_browser(path: str | None = None) -> dict[str, Any]:
        return reports.browse(path)

    @app.post(f"{API_PREFIX}/reports/browser/directory", tags=["reports"])
    async def report_browser_create_directory(
        payload: DirectoryCreateRequest,
    ) -> dict[str, Any]:
        return reports.create_directory(payload.parent, payload.name)

    @app.get(f"{API_PREFIX}/reports/preview", tags=["reports"])
    async def report_preview(path: str) -> dict[str, Any]:
        return reports.preview_path(path)

    @app.get(f"{API_PREFIX}/reports/inspect", tags=["reports"])
    async def report_inspect(path: str) -> dict[str, Any]:
        return reports.inspect_source(path)

    @app.get(f"{API_PREFIX}/reports/experiment-preview", tags=["reports"])
    async def report_experiment_preview(path: str) -> dict[str, Any]:
        from pisa_sample_tools.evidence.builder import preview_experiment

        source = policy.resolve(path, field="path", kind="directory")
        try:
            preview = preview_experiment(source)
        except (OSError, ValueError) as exc:
            raise APIError(400, "experiment_preview_failed", str(exc)) from exc
        def safe(value: Any) -> str:
            return "-".join(filter(None, re.split(r"[^A-Za-z0-9._-]+", str(value or "unknown"))))
        preview["suggested_report_name"] = "_".join(
            [safe(preview.get("scenario_name")), safe(preview.get("simulator")),
             safe(preview.get("av")), f"{safe(preview.get('sampler'))}{preview.get('run_count') or 0}"]
        )
        return preview

    @app.post(f"{API_PREFIX}/reports/compatibility", tags=["reports"])
    async def report_compatibility(payload: ReportValidateRequest) -> dict[str, Any]:
        from pisa_sample_tools.evidence.builder import compare_experiments, preview_experiment

        previews = []
        for index, experiment in enumerate(payload.experiments):
            source = policy.resolve(str(experiment.get("results") or ""), field=f"experiments.{index}.results", kind="directory")
            preview = preview_experiment(source, experiment.get("metadata") or {})
            preview["dataset_id"] = str(experiment.get("id") or preview.get("dataset_id") or source.name)
            previews.append(preview)
        return compare_experiments(previews)

    @app.post(f"{API_PREFIX}/reports/validate", tags=["reports"])
    async def report_validate(payload: ReportValidateRequest) -> dict[str, Any]:
        try:
            from pisa_sample_tools.evidence.builder import default_spec, validate_builder_request
        except ImportError as exc:
            raise APIError(
                501, "reporting_unavailable", "the reporting subsystem is not installed"
            ) from exc
        experiments = payload.experiments
        source: Path | None = None
        if payload.path:
            source = policy.resolve(payload.path, field="path", kind="directory")
            experiments = [{"id": source.name, "results": str(source)}]
        for index, experiment in enumerate(experiments):
            if "results" not in experiment:
                raise APIError(
                    422,
                    "validation_error",
                    "experiment must define results",
                    field=f"experiments.{index}.results",
                )
            resolved = policy.resolve(
                str(experiment["results"]),
                field=f"experiments.{index}.results",
                kind="directory",
            )
            experiment["results"] = str(resolved)
        try:
            validation = validate_builder_request(
                experiments, payload.spec or default_spec(), deep=payload.deep
            )
            if source is None:
                return validation
            return {
                **validation,
                "id": report_id(source),
                "name": source.name,
                "path": str(source),
                "experiment_count": len(experiments),
                "run_count": int(validation.get("run_count") or 0),
                "status": "ready",
            }
        except (OSError, ValueError) as exc:
            raise APIError(400, "report_validation_failed", str(exc)) from exc

    def submit_report_build(
        payload: ReportBuildRequest | ReportPreviewBuildRequest,
        output: Path,
        *,
        overwrite: bool,
        job_kind: str,
        temporary_name: str | None = None,
    ) -> Job:
        experiment_paths = [str(item.get("results") or "") for item in payload.experiments]
        results = [
            policy.resolve(path, field=f"results_paths.{index}", kind="directory")
            for index, path in enumerate(payload.results_paths or experiment_paths)
        ]
        campaign = _optional_file(policy, payload.campaign_path, "campaign_path")
        spec = _optional_file(policy, payload.spec_path, "spec_path")
        if campaign is not None:
            _validate_campaign_paths(campaign, policy)

        normalized = payload.engine == "normalized" or (
            payload.engine == "auto"
            and campaign is None
            and spec is None
            and payload.sensitivity is None
            and payload.validation_mode is None
            and not payload.deep_validation
        )

        def run(context: JobContext) -> Any:
            try:
                if normalized:
                    from pisa_sample_tools.reporting import build_report_bundle

                    context.progress(
                        "indexing_report", message="Discovering and indexing experiment results"
                    )
                    result = build_report_bundle(
                        results, output, overwrite=overwrite,
                        progress=lambda phase, current, total, message: context.progress(
                            phase, current=current, total=total, unit="stages", message=message
                        ),
                    )
                    report_root = output
                    report_path = result.report_path
                    index_path = result.index_path
                else:
                    try:
                        from pisa_sample_tools.evidence.service import build_evidence
                    except ImportError as exc:
                        raise RuntimeError("the reporting subsystem is not installed") from exc
                    result = build_evidence(
                        results_paths=results or None,
                        campaign_path=campaign,
                        output_dir=output,
                        spec_path=spec,
                        overwrite=overwrite,
                        progress=lambda message: context.progress("building_report", message=message),
                        validation_mode=payload.validation_mode,
                        deep_validation=payload.deep_validation,
                        report_mode=payload.report_mode,
                        sensitivity=payload.sensitivity,
                    )
                    context.progress("indexing_report", message="Building the paginated report index")
                    report_root = result.output_dir
                    report_path = result.report_path
                    index_path = ensure_report_index(report_root)
                context.artifact(report_path, kind="report")
                context.artifact(index_path, kind="report_index")
                preview = (
                    reports.register_temporary(report_root, name=temporary_name)
                    if temporary_name is not None
                    else reports.preview(report_root)
                )
                if temporary_name is None:
                    reports.scan(report_root)
                return {
                    "report_path": str(report_path),
                    "index_path": str(index_path),
                    "report_id": preview["id"],
                    "storage_kind": preview["storage_kind"],
                }
            except Exception:
                if temporary_name is not None:
                    shutil.rmtree(output, ignore_errors=True)
                raise

        return jobs.submit(job_kind, payload.model_dump(), run)

    @app.post(
        f"{API_PREFIX}/reports/build", response_model=Job, status_code=202, tags=["reports"]
    )
    async def report_build(payload: ReportBuildRequest) -> Job:
        output = policy.resolve(
            payload.output_dir, field="output_dir", must_exist=False, kind="directory"
        )
        return submit_report_build(
            payload, output, overwrite=payload.overwrite, job_kind="report_build"
        )

    @app.post(
        f"{API_PREFIX}/reports/previews", response_model=Job, status_code=202, tags=["reports"]
    )
    async def report_preview_build(payload: ReportPreviewBuildRequest) -> Job:
        output = reports.temporary_output(payload.report_name)
        return submit_report_build(
            payload, output, overwrite=False, job_kind="report_preview", temporary_name=payload.report_name
        )

    @app.post(
        f"{API_PREFIX}/reports/{{identifier}}/rebuild",
        response_model=Job,
        status_code=202,
        tags=["reports"],
    )
    async def report_rebuild(identifier: str, payload: LegacyRebuildRequest) -> Job:
        source = reports.get(identifier)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output_value = str(source) if payload.overwrite else payload.output_dir or str(
            source.with_name(f"{source.name}--rebuilt-{timestamp}")
        )
        output = policy.resolve(
            output_value, field="output_dir", must_exist=False, kind="directory"
        )
        if output == source and not payload.overwrite:
            raise APIError(
                409,
                "non_destructive_rebuild_required",
                "legacy rebuild output must differ from the source report",
                field="output_dir",
            )
        import yaml

        input_manifest: dict[str, Any] = {}
        for input_manifest_path in (
            source / "provenance" / "input_manifest.yaml",
            source / "provenance" / "input_manifest.json",
        ):
            if not input_manifest_path.is_file():
                continue
            try:
                loaded = (
                    json.loads(input_manifest_path.read_text(encoding="utf-8"))
                    if input_manifest_path.suffix == ".json"
                    else yaml.safe_load(input_manifest_path.read_text(encoding="utf-8"))
                )
            except (OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
                raise APIError(
                    409,
                    "legacy_rebuild_unavailable",
                    f"the recorded input manifest cannot be read: {exc}",
                ) from exc
            if isinstance(loaded, dict):
                input_manifest = loaded
                break
        recorded_inputs = [str(value) for value in input_manifest.get("inputs") or []]
        for dataset in input_manifest.get("datasets") or []:
            if not isinstance(dataset, dict):
                continue
            value = dataset.get("results") or dataset.get("results_path")
            if value:
                recorded_inputs.append(str(value))
        # Normalized portable provenance intentionally omits absolute host paths.
        # The read-only report index remains the authoritative local source link.
        if not recorded_inputs:
            index_path = source / "report" / "index.sqlite"
            if index_path.is_file():
                try:
                    with sqlite3.connect(
                        f"file:{index_path.as_posix()}?mode=ro", uri=True
                    ) as connection:
                        recorded_inputs.extend(
                            str(row[0])
                            for row in connection.execute(
                                "SELECT source_path FROM datasets ORDER BY dataset_id"
                            ).fetchall()
                            if row[0]
                        )
                except sqlite3.DatabaseError as exc:
                    raise APIError(
                        409,
                        "legacy_rebuild_unavailable",
                        f"the normalized report index cannot recover its record folders: {exc}",
                    ) from exc
        inputs = [
            policy.resolve(value, field=f"inputs.{index}", kind="directory")
            for index, value in enumerate(dict.fromkeys(recorded_inputs))
        ]
        campaign = source / "provenance" / "resolved_campaign.yaml"
        campaign_path = campaign if campaign.is_file() else None
        if campaign_path is not None:
            _validate_campaign_paths(campaign_path, policy)
        if campaign_path is None and not inputs:
            raise APIError(
                409,
                "legacy_rebuild_unavailable",
                "report source record folders are missing from provenance and the normalized index",
            )

        if campaign_path is not None:
            try:
                from pisa_sample_tools.evidence.campaign import load_campaign

                inputs = [
                    policy.resolve(
                        dataset.results_path,
                        field=f"campaign.datasets.{index}.results",
                        kind="directory",
                    )
                    for index, dataset in enumerate(load_campaign(campaign_path))
                ]
            except (OSError, ValueError) as exc:
                raise APIError(
                    409,
                    "legacy_rebuild_unavailable",
                    f"the recorded campaign cannot be resolved: {exc}",
                ) from exc

        def run(context: JobContext) -> Any:
            from pisa_sample_tools.reporting import build_report_bundle

            context.progress(
                "rebuilding_report",
                message="Re-indexing recorded sources into the current normalized report format",
            )
            result = build_report_bundle(
                inputs,
                output_dir=output,
                overwrite=payload.overwrite,
                lineage={
                    "operation": "in_place_legacy_rebuild" if payload.overwrite else "non_destructive_legacy_rebuild",
                    "source_report": str(source),
                    "source_report_id": identifier,
                },
                progress=lambda phase, current, total, message: context.progress(
                    phase, current=current, total=total, unit="stages", message=message
                ),
            )
            lineage_path = output / "provenance" / "rebuild_lineage.json"
            lineage_path.write_text(
                json.dumps(
                    {
                        "source_report": str(source),
                        "source_report_id": identifier,
                        "rebuilt_at": datetime.now(UTC).isoformat(),
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            context.artifact(result.report_path, kind="report")
            context.artifact(result.index_path, kind="report_index")
            reports.scan(output)
            return {
                "report_path": str(result.report_path),
                "index_path": str(result.index_path),
                "report_id": reports.preview(output)["id"],
            }

        return jobs.submit("report_rebuild", payload.model_dump(), run)

    @app.get(f"{API_PREFIX}/reports/{{identifier}}/details", tags=["reports"])
    async def report_details(identifier: str) -> dict[str, Any]:
        return reports.details(identifier)

    @app.get(f"{API_PREFIX}/reports/{{identifier}}/preview", tags=["reports"])
    async def report_preview_by_id(identifier: str) -> dict[str, Any]:
        return reports.preview(reports.get(identifier))

    @app.post(f"{API_PREFIX}/reports/{{identifier}}/lease", tags=["reports"])
    async def report_preview_lease(identifier: str) -> dict[str, Any]:
        return reports.lease_temporary(identifier)

    @app.delete(f"{API_PREFIX}/reports/{{identifier}}/preview", tags=["reports"])
    async def report_preview_discard(identifier: str) -> Response:
        reports.discard_temporary(identifier)
        return Response(status_code=204)

    @app.post(
        f"{API_PREFIX}/reports/{{identifier}}/persist",
        response_model=Job,
        status_code=202,
        tags=["reports"],
    )
    async def report_preview_persist(
        identifier: str, payload: ReportPersistRequest
    ) -> Job:
        reports.get(identifier)
        target = policy.resolve(
            payload.output_dir, field="output_dir", must_exist=False, kind="directory"
        )

        def run(context: JobContext) -> dict[str, Any]:
            context.progress("saving_report", current=0, total=1, unit="report", message="Moving the preview into the report library")
            preview = reports.persist_temporary(identifier, target, overwrite=payload.overwrite)
            context.progress("saving_report", current=1, total=1, unit="report", message="Saved report is ready")
            context.artifact(target / "report" / "analysis_report.html", kind="report")
            reports.scan(target)
            return {
                "report_path": str(target / "report" / "analysis_report.html"),
                "index_path": str(target / "report" / "index.sqlite"),
                "report_id": preview["id"],
                "storage_kind": "saved",
            }

        return jobs.submit("report_persist", payload.model_dump(), run)

    @app.post(f"{API_PREFIX}/reports/{{identifier}}/rename", tags=["reports"])
    async def report_rename(
        identifier: str, payload: ReportRenameRequest
    ) -> dict[str, Any]:
        return reports.rename(identifier, payload.new_name)

    @app.delete(f"{API_PREFIX}/reports/{{identifier}}", tags=["reports"])
    async def report_delete(
        identifier: str, payload: ReportDeleteRequest
    ) -> Response:
        reports.delete(identifier, payload.confirm_name)
        return Response(status_code=204)

    @app.get(f"{API_PREFIX}/reports/{{identifier}}/scatter", tags=["reports"])
    async def report_scatter(
        identifier: str,
        x: str | None = Query(default=None, max_length=240),
        y: str | None = Query(default=None, max_length=240),
        color: str | None = Query(default="outcome", max_length=240),
        filter_field: str | None = Query(default=None, max_length=240),
        dataset: str | None = Query(default=None, max_length=240),
        stop_reason: str | None = Query(default=None, max_length=500),
        limit: int | None = Query(default=None, ge=100, le=1_000_000),
    ) -> dict[str, Any]:
        return reports.scatter(
            identifier, x=x, y=y, color=color, filter_field=filter_field, dataset=dataset,
            stop_reason=stop_reason, limit=limit
        )

    @app.get(f"{API_PREFIX}/reports/{{identifier}}/overview", tags=["reports"])
    async def report_overview(identifier: str) -> dict[str, Any]:
        return reports.overview(identifier)

    @app.get(f"{API_PREFIX}/reports/{{identifier}}/index", tags=["reports"])
    async def report_index(identifier: str) -> dict[str, Any]:
        return reports.index_info(identifier)

    @app.get(f"{API_PREFIX}/reports/{{identifier}}/runs", tags=["reports"])
    async def report_runs(
        identifier: str,
        cursor: str | None = None,
        limit: int = Query(default=100, ge=1, le=1_000),
        outcome: str | None = None,
        experiment: str | None = None,
        query: str | None = Query(default=None, max_length=200),
        sort: str | None = Query(default=None, max_length=100),
        descending: bool = False,
    ) -> dict[str, Any]:
        return reports.runs(
            identifier,
            cursor=cursor,
            limit=limit,
            outcome=outcome,
            experiment=experiment,
            query=query,
            sort=sort,
            descending=descending,
        )

    @app.get(f"{API_PREFIX}/reports/{{identifier}}/charts", tags=["reports"])
    async def report_charts(identifier: str, section: str | None = None) -> dict[str, Any]:
        root = reports.get(identifier)
        try:
            from .visualizations import VisualizationError, build_visualizations

            visualizations = build_visualizations(root, section=section or "all")
        except (VisualizationError, sqlite3.DatabaseError):
            visualizations = []
        items = reports.artifacts(identifier)
        if section:
            needle = section.casefold().replace("_", "-")
            items = [
                item
                for item in items
                if needle in item["path"].casefold().replace("_", "-")
            ]
        return {
            "items": items,
            "visualizations": visualizations,
            "section": section,
            "export_formats": ["svg", "pdf", "png", "csv", "json"],
        }

    @app.get(f"{API_PREFIX}/reports/{{identifier}}/comparisons", tags=["reports"])
    async def report_comparisons(identifier: str) -> dict[str, Any]:
        return reports.comparisons(identifier)

    @app.post(
        f"{API_PREFIX}/reports/{{identifier}}/comparisons/{{relation_id}}/parameter-analysis",
        tags=["reports"],
    )
    async def report_paired_parameter_analysis(
        identifier: str,
        relation_id: str,
        payload: PairedParameterAnalysisRequest,
    ) -> dict[str, Any]:
        return reports.paired_parameter_analysis(
            identifier, relation_id, payload.model_dump(exclude_none=True)
        )

    @app.post(
        f"{API_PREFIX}/reports/{{identifier}}/comparisons/{{relation_id}}/metric-agreement",
        tags=["reports"],
    )
    async def report_paired_metric_agreement(
        identifier: str,
        relation_id: str,
        payload: PairedMetricAgreementRequest,
    ) -> dict[str, Any]:
        return reports.paired_metric_agreement(
            identifier, relation_id, payload.model_dump(exclude_none=True)
        )

    @app.get(f"{API_PREFIX}/reports/{{identifier}}/consistency", tags=["reports"])
    async def report_consistency(
        identifier: str,
        profile: Annotated[
            Literal["trajectory_outlier_controls", "full_controls"], Query()
        ] = "trajectory_outlier_controls",
        position_tolerances_m: Annotated[list[float] | None, Query()] = None,
        outlier_limit: Annotated[int, Query(ge=1, le=1_000)] = 25,
    ) -> dict[str, Any]:
        from pisa_sample_tools.reporting import (
            build_quick_consistency,
            deep_consistency_status,
        )

        config = ConsistencyAnalyzeRequest(
            profile=profile,
            position_tolerances_m=position_tolerances_m or [0.001, 0.01, 0.1],
            outlier_limit=outlier_limit,
        )
        root = reports.get(identifier)
        index_path = root / "report" / "index.sqlite"
        quick_path = root / "summary" / "consistency.json"
        try:
            quick_value = json.loads(quick_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            quick_value = (
                build_quick_consistency(index_path)
                if index_path.is_file()
                else {
                    "schema_version": 1,
                    "available": False,
                    "reason": "normalized_report_index_required",
                    "dataset_count": 0,
                    "canonical_dataset_count": 0,
                    "group_count": 0,
                    "groups": [],
                    "excluded_duplicate_aliases": [],
                }
            )
        quick = quick_value if isinstance(quick_value, dict) else {}
        if index_path.is_file():
            try:
                deep = deep_consistency_status(
                    root,
                    profile=config.profile,
                    position_tolerances_m=config.position_tolerances_m,
                    outlier_limit=config.outlier_limit,
                )
            except (OSError, ValueError):
                deep = {
                    "state": "not_generated",
                    "reason": "report_source_fingerprint_required",
                    "profile": config.profile,
                    "position_tolerances_m": config.position_tolerances_m,
                    "outlier_limit": config.outlier_limit,
                    "summary": None,
                    "artifacts": [],
                }
        else:
            deep = {
                "state": "not_generated",
                "reason": "normalized_report_index_required",
                "profile": config.profile,
                "position_tolerances_m": config.position_tolerances_m,
                "outlier_limit": config.outlier_limit,
                "summary": None,
                "artifacts": [],
            }
        if deep.get("state") == "ready":
            cache_key = str(deep.get("cache_key") or "")
            deep["artifacts"] = [
                {
                    "path": f"consistency/derived/{cache_key}/{artifact}",
                    "download_url": (
                        f"{API_PREFIX}/reports/{identifier}/artifacts/"
                        f"consistency/derived/{cache_key}/{artifact}"
                    ),
                }
                for artifact in deep.get("artifacts", [])
            ]
        active = next(
            (
                job
                for job in jobs.list(limit=1_000)
                if job.kind == "consistency_analysis"
                and job.request.get("report_id") == identifier
                and job.request.get("profile") == config.profile
                and job.request.get("position_tolerances_m")
                == config.position_tolerances_m
                and job.request.get("outlier_limit") == config.outlier_limit
                and job.status in {"queued", "running"}
            ),
            None,
        )
        if active is not None:
            deep = {
                **deep,
                "state": active.status,
                "job": active.model_dump(mode="json"),
            }
        return {"quick": quick, "deep": deep}

    @app.post(
        f"{API_PREFIX}/reports/{{identifier}}/consistency/analyze",
        response_model=Job,
        status_code=202,
        tags=["reports"],
    )
    async def analyze_report_consistency(
        identifier: str, payload: ConsistencyAnalyzeRequest
    ) -> Job:
        from pisa_sample_tools.reporting import analyze_deep_consistency

        root = reports.get(identifier)
        request_data = {"report_id": identifier, **payload.model_dump()}
        existing = next(
            (
                job
                for job in jobs.list(limit=1_000)
                if job.kind == "consistency_analysis"
                and job.request == request_data
                and job.status in {"queued", "running"}
            ),
            None,
        )
        if existing is not None:
            return existing

        def run(context: JobContext) -> dict[str, Any]:
            result = analyze_deep_consistency(
                root,
                profile=payload.profile,
                position_tolerances_m=payload.position_tolerances_m,
                outlier_limit=payload.outlier_limit,
                force=payload.force,
                progress=lambda phase, current, total, message, stage, stages: context.progress(
                    phase,
                    current=current,
                    total=total,
                    unit=("files" if "files" in message else "samples" if "samples" in message else "artifacts" if phase == "writing_artifacts" else "items"),
                    message=f"Phase {stage} / {stages} · {message}",
                ),
                check_cancelled=context.check_cancelled,
                resolve_trace=lambda path: policy.resolve(
                    path,
                    field="consistency.trace",
                    kind="file",
                    suffixes={".csv"},
                ),
            )
            artifacts = []
            for raw_path in result.get("artifacts", []):
                path = Path(str(raw_path)).resolve()
                context.artifact(path, kind="consistency")
                relative = path.relative_to(root).as_posix()
                artifacts.append(
                    {
                        "path": relative,
                        "download_url": (
                            f"{API_PREFIX}/reports/{identifier}/artifacts/{relative}"
                        ),
                    }
                )
            return {**result, "artifacts": artifacts, "report_id": identifier}

        return jobs.submit("consistency_analysis", request_data, run)

    @app.get(f"{API_PREFIX}/reports/{{identifier}}/cases/{{run_id:path}}", tags=["reports"])
    async def report_case(
        identifier: str,
        run_id: str,
        maximum_points: int = Query(default=5_000, ge=100, le=100_000),
        include_map: bool = True,
    ) -> dict[str, Any]:
        return reports.case_detail(identifier, run_id, maximum_points=maximum_points, include_map=include_map)

    @app.get(f"{API_PREFIX}/reports/{{identifier}}/media", tags=["reports"])
    async def report_media(identifier: str) -> dict[str, Any]:
        from .media import media_capabilities

        return {
            "items": reports.artifacts(identifier, media=True),
            "derived_media_label": "reconstructed schematic",
            "capabilities": media_capabilities(),
        }

    @app.post(
        f"{API_PREFIX}/reports/{{identifier}}/media", status_code=202, tags=["reports"]
    )
    async def create_report_media(identifier: str, payload: MediaCreateRequest) -> Job:
        root = reports.get(identifier)

        def run(context: JobContext) -> dict[str, Any]:
            from .media import generate_schematic_media

            context.progress(
                "rendering_schematic",
                message="Reconstructing a clearly labeled schematic from indexed traces",
            )
            result = generate_schematic_media(
                root,
                payload.run_id,
                run_ids=payload.run_ids,
                format=payload.format,
                fps=payload.fps,
                max_frames=payload.max_frames,
                playback_rate=payload.playback_rate,
                size=(payload.width, payload.height),
                overwrite=payload.overwrite,
                include_map=payload.include_map,
                map_reference=payload.map_reference,
                map_boundaries=payload.map_boundaries,
                map_junctions=payload.map_junctions,
                show_bounding_boxes=payload.show_bounding_boxes,
                follow_cursor=payload.follow_cursor,
                trail_only=payload.trail_only,
                render_mode=payload.render_mode,
                show_ego=payload.show_ego,
                show_agents=payload.show_agents,
                show_goal=payload.show_goal,
                show_grid=payload.show_grid,
            )
            context.artifact(result.media_path, kind="reconstructed_schematic")
            context.artifact(result.metadata_path, kind="media_provenance")
            media_path = result.media_path.relative_to(root).as_posix()
            metadata_path = result.metadata_path.relative_to(root).as_posix()
            return {
                "run_id": result.run_id,
                "format": result.format,
                "path": media_path,
                "metadata_path": metadata_path,
                "download_url": (
                    f"{API_PREFIX}/reports/{identifier}/artifacts/{media_path}"
                ),
                "data_sha256": result.data_sha256,
                "frame_count": result.frame_count,
                "rendered_frame_count": result.rendered_frame_count,
                "fps": result.fps,
                "width": result.width,
                "height": result.height,
                "cached": result.cached,
                "label": "Reconstructed schematic — not camera footage",
            }

        return jobs.submit("schematic_media", payload.model_dump(), run)

    @app.post(f"{API_PREFIX}/tools/animation/transcode", tags=["tools"])
    async def transcode_browser_animation(
        request: Request,
        format: str = Query(pattern="^(gif|mp4)$"),
    ) -> Response:
        """Convert a browser-recorded WebM visualization without retaining the upload."""

        payload = await request.body()
        if not payload:
            raise APIError(400, "empty_animation", "the recorded animation is empty")
        if len(payload) > 100 * 1024 * 1024:
            raise APIError(413, "animation_too_large", "animation uploads are limited to 100 MiB")
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise APIError(501, "encoder_unavailable", "ffmpeg is required for GIF/MP4 export")
        with tempfile.TemporaryDirectory(prefix="pisa-animation-", dir="/tmp") as raw:
            source = Path(raw) / "source.webm"
            destination = Path(raw) / f"animation.{format}"
            source.write_bytes(payload)
            if format == "mp4":
                command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(destination)]
                media_type = "video/mp4"
            else:
                gif_filter = "fps=20,scale=1600:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=256:stats_mode=diff[p];[s1][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle"
                command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-filter_complex", gif_filter, "-loop", "0", str(destination)]
                media_type = "image/gif"
            completed = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
            if completed.returncode != 0 or not destination.is_file():
                raise APIError(500, "animation_transcode_failed", completed.stderr.strip()[-1000:] or "ffmpeg did not produce an artifact")
            return Response(
                content=destination.read_bytes(),
                media_type=media_type,
                headers={"Content-Disposition": f'attachment; filename="sampling-animation.{format}"'},
            )

    @app.post(
        f"{API_PREFIX}/reports/{{identifier}}/export", status_code=202, tags=["reports"]
    )
    @app.post(
        f"{API_PREFIX}/reports/{{identifier}}/exports", status_code=202, tags=["reports"]
    )
    async def report_export(identifier: str, payload: ExportRequest) -> Any:
        if payload.visualization_id:
            root = reports.get(identifier)

            def run(context: JobContext) -> Any:
                from .visualizations import export_visualization

                context.progress("rendering", message="Rendering publication export")
                result = export_visualization(
                    root,
                    payload.visualization_id or "",
                    format=payload.format or "",
                    preset=payload.preset or "paper-single",
                    dpi=payload.dpi,
                    background=payload.background,
                )
                context.artifact(root / result["path"], kind="visualization_export")
                return {
                    **result,
                    "download_url": (
                        f"{API_PREFIX}/reports/{identifier}/artifacts/{result['path']}"
                    ),
                }

            return jobs.submit("report_export", payload.model_dump(), run)

        source = reports.artifact(identifier, payload.artifact_path or "")
        target = source
        if payload.format and source.suffix.lower() != f".{payload.format}":
            candidate = source.with_suffix(f".{payload.format}")
            root = reports.get(identifier)
            if candidate.is_relative_to(root) and candidate.is_file():
                target = candidate
            else:
                raise APIError(
                    501,
                    "export_format_unavailable",
                    "the requested pre-rendered format is not available for this artifact",
                    details={"available": source.suffix.lower().lstrip(".")},
                )
        relative = target.relative_to(reports.get(identifier)).as_posix()
        return {
            "path": relative,
            "format": target.suffix.lower().lstrip("."),
            "size": target.stat().st_size,
            "download_url": f"{API_PREFIX}/reports/{identifier}/artifacts/{relative}",
        }

    @app.get(f"{API_PREFIX}/reports/{{identifier}}/snapshot", tags=["reports"])
    async def report_snapshot(identifier: str) -> FileResponse:
        root = reports.get(identifier)
        path = root / "report" / "analysis_report.html"
        if not path.is_file():
            raise APIError(501, "snapshot_unavailable", "this report has no portable snapshot")
        return FileResponse(
            path,
            media_type="text/html",
            headers={
                "Content-Security-Policy": (
                    "sandbox allow-scripts; default-src 'none'; "
                    "style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:"
                ),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post(f"{API_PREFIX}/reports/{{identifier}}/snapshot", tags=["reports"])
    async def prepare_report_snapshot(
        identifier: str, payload: SnapshotRequest | None = None
    ) -> dict[str, Any]:
        root = reports.get(identifier)
        path = root / "report" / "analysis_report.html"
        if not path.is_file():
            raise APIError(501, "snapshot_unavailable", "this report has no portable snapshot")
        return {
            "available": True,
            "path": "report/analysis_report.html",
            "url": f"{API_PREFIX}/reports/{identifier}/snapshot",
            "portable": True,
            "mode": payload.mode if payload else "compact",
            "selected_run_ids": payload.selected_run_ids if payload else [],
        }

    @app.get(
        f"{API_PREFIX}/reports/{{identifier}}/artifacts/{{artifact_path:path}}", tags=["reports"]
    )
    async def report_artifact(identifier: str, artifact_path: str) -> FileResponse:
        path = reports.artifact(identifier, artifact_path)
        active = path.suffix.casefold() == ".html"
        return FileResponse(
            path,
            media_type=mimetypes.guess_type(path.name)[0],
            filename=path.name if active else None,
            content_disposition_type="attachment" if active else "inline",
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox; default-src 'none'",
            },
        )

    @app.get(f"{API_PREFIX}/runner/capabilities", tags=["experiments"])
    async def runner_capabilities() -> dict[str, Any]:
        return {
            "available": runner_store is not None,
            "configured": config is not None,
            "actions": ["build", "start", "run", "run_all", "report"],
            "resource_cleanup": True,
        }

    @app.get(f"{API_PREFIX}/runner/registry", tags=["experiments"])
    async def runner_registry() -> dict[str, Any]:
        store = _require_runner(runner_store)
        try:
            return {"path": str(store.path.resolve()), "registry": store.editable()}
        except (OSError, ValueError) as exc:
            raise APIError(400, "runner_config_invalid", str(exc)) from exc

    @app.put(f"{API_PREFIX}/runner/registry", tags=["experiments"])
    async def runner_registry_save(payload: RunnerRegistryRequest) -> dict[str, Any]:
        store = _require_runner(runner_store)
        try:
            store.save(payload.registry)
            return {"path": str(store.path.resolve()), "registry": store.editable()}
        except (OSError, ValueError) as exc:
            raise APIError(400, "runner_config_invalid", str(exc)) from exc

    @app.get(f"{API_PREFIX}/runner/presets", tags=["experiments"])
    async def runner_presets() -> dict[str, Any]:
        store = _require_runner(runner_store)
        try:
            registry = store.editable()
        except (OSError, ValueError) as exc:
            raise APIError(400, "runner_config_invalid", str(exc)) from exc
        return {
            "experiments": registry.get("experiments", {}),
            "items": [
                {"id": identifier, **value}
                for identifier, value in registry.get("experiments", {}).items()
            ],
            "components": registry.get("components", {}),
            "version": registry.get("version"),
        }

    @app.post(f"{API_PREFIX}/runner/presets", tags=["experiments"])
    async def runner_preset_create(payload: RunnerPresetCreateRequest) -> dict[str, Any]:
        store = _require_runner(runner_store)
        try:
            experiment = store.create_preset(
                payload.preset_id,
                template_id=payload.template_id,
                label=payload.label,
                simulator_component=payload.simulator_component,
                av_component=payload.av_component,
                tags=payload.tags,
            )
            return {"preset_id": payload.preset_id.strip(), "experiment": experiment}
        except (OSError, ValueError) as exc:
            raise APIError(400, "runner_preset_create_failed", str(exc)) from exc

    @app.put(f"{API_PREFIX}/runner/presets/{{preset_id}}", tags=["experiments"])
    async def runner_preset_update(
        preset_id: str, payload: RunnerPresetUpdateRequest
    ) -> dict[str, Any]:
        store = _require_runner(runner_store)
        try:
            return {
                "preset_id": preset_id,
                "experiment": store.update_preset(preset_id, payload.experiment),
            }
        except (OSError, ValueError) as exc:
            raise APIError(400, "runner_preset_update_failed", str(exc)) from exc

    @app.post(f"{API_PREFIX}/runner/presets/{{preset_id}}/rename", tags=["experiments"])
    async def runner_preset_rename(
        preset_id: str, payload: RunnerPresetRenameRequest
    ) -> dict[str, Any]:
        store = _require_runner(runner_store)
        try:
            return store.rename_preset(
                preset_id, new_id=payload.new_id, label=payload.label
            )
        except (OSError, ValueError) as exc:
            raise APIError(400, "runner_preset_rename_failed", str(exc)) from exc

    @app.post(f"{API_PREFIX}/runner/presets/{{preset_id}}/delete", tags=["experiments"])
    async def runner_preset_delete(
        preset_id: str, payload: ConfirmationRequest
    ) -> dict[str, Any]:
        if not payload.confirm:
            raise APIError(
                409,
                "confirmation_required",
                "explicit preset deletion confirmation is required",
                field="confirm",
            )
        store = _require_runner(runner_store)
        try:
            store.delete_preset(preset_id)
            return {"deleted": preset_id}
        except (OSError, ValueError) as exc:
            raise APIError(400, "runner_preset_delete_failed", str(exc)) from exc

    @app.post(f"{API_PREFIX}/runner/scenarios/inspect", tags=["experiments"])
    async def runner_scenario_inspect(payload: RunnerScenarioRequest) -> dict[str, Any]:
        path = policy.resolve(payload.path, field="path", kind="directory")
        try:
            from pisa_sample_tools.experiment_runner.scenario import inspect_scenario_directory

            return inspect_scenario_directory(path)
        except (OSError, ValueError) as exc:
            raise APIError(400, "scenario_inspection_failed", str(exc)) from exc

    @app.post(f"{API_PREFIX}/runner/experiments/resolve", tags=["experiments"])
    async def runner_resolve(payload: RunnerExperimentRequest) -> dict[str, Any]:
        store = _require_runner(runner_store)
        try:
            return store.resolve_experiment(payload.experiment_id, payload.overrides)
        except (OSError, ValueError) as exc:
            raise APIError(400, "runner_resolve_failed", str(exc)) from exc

    @app.post(f"{API_PREFIX}/runner/experiments/validate", tags=["experiments"])
    async def runner_validate(payload: RunnerExperimentRequest) -> dict[str, Any]:
        store = _require_runner(runner_store)
        try:
            from pisa_sample_tools.experiment_runner.orchestrator import validate_experiment

            experiment = store.resolve_experiment(payload.experiment_id, payload.overrides)
            return validate_experiment(experiment, check_runtime=False)
        except (OSError, ValueError) as exc:
            raise APIError(400, "runner_validation_failed", str(exc)) from exc

    @app.post(f"{API_PREFIX}/runner/experiments/preview", tags=["experiments"])
    async def runner_preview(payload: RunnerExperimentRequest) -> dict[str, Any]:
        store = _require_runner(runner_store)
        try:
            from pisa_sample_tools.experiment_runner.commands import (
                allocate_ports,
                build_command,
                common_mounts,
                docker_run_command,
            )
            from pisa_sample_tools.experiment_runner.orchestrator import validate_experiment
            from pisa_sample_tools.experiment_runner.spec import build_runner_spec

            experiment = store.resolve_experiment(payload.experiment_id, payload.overrides)
            output = Path(experiment["task"]["output_dir"]).expanduser().resolve()
            ports = {role: allocate_ports(experiment[role]) for role in ("simulator", "av")}
            mounts = common_mounts(experiment, output)
            commands: dict[str, Any] = {"build": {}, "run": {}}
            for role in ("simulator", "av"):
                commands["build"][role] = build_command(experiment[role])
                commands["run"][role] = docker_run_command(
                    experiment[role],
                    role=role,
                    job_id="preview",
                    ports=ports[role],
                    mounts=mounts,
                )[0]
            return {
                "experiment": experiment,
                "ports": ports,
                "commands": commands,
                "runner_spec": build_runner_spec(experiment, ports, output, "preview"),
                "validation": validate_experiment(experiment, check_ports=False),
            }
        except (OSError, ValueError) as exc:
            raise APIError(400, "runner_preview_failed", str(exc)) from exc

    @app.get(f"{API_PREFIX}/runner/jobs", tags=["experiments"])
    async def runner_jobs() -> dict[str, Any]:
        manager = _require_runner(runner_manager)
        items = [_canonical_runner_job(item.payload()) for item in manager.jobs.values()]
        return {"jobs": items, "items": items}

    @app.post(f"{API_PREFIX}/runner/jobs", status_code=202, tags=["experiments"])
    async def runner_submit(payload: RunnerJobRequest) -> dict[str, Any]:
        store = _require_runner(runner_store)
        manager = _require_runner(runner_manager)
        try:
            experiment = store.resolve_experiment(payload.experiment_id, payload.overrides)
            return _canonical_runner_job(manager.submit(experiment, payload.action).payload())
        except (OSError, ValueError) as exc:
            raise APIError(400, "runner_submit_failed", str(exc)) from exc

    @app.get(f"{API_PREFIX}/runner/jobs/{{job_id}}", tags=["experiments"])
    async def runner_job(job_id: str) -> dict[str, Any]:
        manager = _require_runner(runner_manager)
        try:
            return _canonical_runner_job(manager.get(job_id).payload())
        except (KeyError, ValueError) as exc:
            raise APIError(404, "runner_job_not_found", "experiment job was not found") from exc

    @app.post(f"{API_PREFIX}/runner/jobs/{{job_id}}/cancel", tags=["experiments"])
    async def runner_cancel(job_id: str) -> dict[str, Any]:
        manager = _require_runner(runner_manager)
        try:
            return _canonical_runner_job(manager.cancel(job_id).payload())
        except (KeyError, ValueError) as exc:
            raise APIError(404, "runner_job_not_found", "experiment job was not found") from exc

    @app.post(f"{API_PREFIX}/runner/jobs/{{job_id}}/resume", tags=["experiments"])
    async def runner_resume(job_id: str, payload: RunnerResumeRequest) -> dict[str, Any]:
        manager = _require_runner(runner_manager)
        try:
            return _canonical_runner_job(manager.resume(job_id, payload.action).payload())
        except (KeyError, ValueError) as exc:
            raise APIError(400, "runner_resume_failed", str(exc)) from exc

    @app.get(f"{API_PREFIX}/runner/resources", tags=["experiments"])
    async def runner_resources() -> dict[str, Any]:
        try:
            from pisa_sample_tools.experiment_runner.orchestrator import stale_containers

            return {"containers": stale_containers()}
        except (OSError, ValueError) as exc:
            raise APIError(503, "container_runtime_unavailable", str(exc)) from exc

    @app.post(f"{API_PREFIX}/runner/resources/cleanup", tags=["experiments"])
    async def runner_resource_cleanup(payload: RunnerCleanupRequest) -> dict[str, Any]:
        try:
            inspected = subprocess.run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    '{{ index .Config.Labels "pisa.experiment-runner" }}',
                    payload.name,
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if inspected.returncode or inspected.stdout.strip() != "true":
                raise APIError(
                    403,
                    "resource_not_owned",
                    "refusing to stop a container not owned by PISA Experiment Runner",
                )
            stopped = subprocess.run(
                ["docker", "stop", payload.name],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if stopped.returncode:
                raise APIError(
                    502,
                    "container_stop_failed",
                    stopped.stderr.strip() or "docker stop failed",
                )
            return {"stopped": payload.name}
        except APIError:
            raise
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise APIError(503, "container_runtime_unavailable", str(exc)) from exc

    @app.post(f"{API_PREFIX}/tools/trajectory/render", tags=["tools"])
    async def trajectory_render(payload: TrajectoryRenderRequest) -> dict[str, Any]:
        from pisa_sample_tools.trajectory.service import render_agent_trajectory_svg

        source = policy.resolve(payload.source_path, field="source_path", kind="file")
        try:
            svg = render_agent_trajectory_svg(
                source,
                title=payload.title,
                width=payload.width,
                height=payload.height,
                x_range=payload.x_range,
                y_range=payload.y_range,
                equal_scale=payload.equal_scale,
                ignore_agent_ids=payload.ignore_agent_ids,
                origin_agent_id=payload.origin_agent_id,
            )
        except (OSError, ValueError) as exc:
            raise APIError(400, "trajectory_render_failed", str(exc)) from exc
        return {"format": "svg", "media_type": "image/svg+xml", "svg": svg}

    @app.post(
        f"{API_PREFIX}/tools/trajectory", response_model=Job, status_code=202, tags=["tools"]
    )
    async def trajectory(payload: TrajectoryRequest) -> Job:
        input_path = policy.resolve(payload.input_path, field="input_path")
        output = policy.resolve(
            payload.output_dir, field="output_dir", must_exist=False, kind="directory"
        )

        def run(context: JobContext) -> Any:
            from pisa_sample_tools.trajectory import visualize_trajectories

            context.progress("rendering", message="Rendering trajectory SVG files")
            result = visualize_trajectories(
                input_path=input_path,
                output_dir=output,
                overwrite=payload.overwrite,
                width=payload.width,
                height=payload.height,
                x_range=payload.x_range,
                y_range=payload.y_range,
                equal_scale=payload.equal_scale,
                ignore_agent_ids=payload.ignore_agent_ids,
                origin_agent_id=payload.origin_agent_id,
            )
            context.artifact(result.manifest_path, kind="manifest")
            return result

        return jobs.submit("trajectory", payload.model_dump(), run)

    @app.post(
        f"{API_PREFIX}/tools/trajectory-compare",
        response_model=Job,
        status_code=202,
        tags=["tools"],
    )
    async def trajectory_compare(payload: TrajectoryCompareRequest) -> Job:
        left = policy.resolve(payload.left_path, field="left_path")
        right = policy.resolve(payload.right_path, field="right_path")
        output = policy.resolve(
            payload.output_dir, field="output_dir", must_exist=False, kind="directory"
        )

        def run(context: JobContext) -> Any:
            from pisa_sample_tools.trajectory_compare import compare_trajectory_sets

            context.progress("comparing", message="Comparing trajectory sets")
            result = compare_trajectory_sets(
                left_path=left,
                right_path=right,
                output_dir=output,
                left_label=payload.left_label,
                right_label=payload.right_label,
                ignore_agent_ids=payload.ignore_agent_ids,
                overwrite=payload.overwrite,
                width=payload.width,
                height=payload.height,
                equal_scale=payload.equal_scale,
            )
            context.artifact(result.summary_csv_path, kind="data")
            return result

        return jobs.submit("trajectory_compare", payload.model_dump(), run)

    @app.post(
        f"{API_PREFIX}/tools/outcome-eval",
        response_model=Job,
        status_code=202,
        tags=["tools"],
    )
    async def outcome_eval(payload: OutcomeEvalRequest) -> Job:
        input_path = policy.resolve(payload.input_path, field="input_path")
        config_path = policy.resolve(payload.config_path, field="config_path", kind="file")
        output = policy.resolve(
            payload.output_dir, field="output_dir", must_exist=False, kind="directory"
        )

        def run(context: JobContext) -> Any:
            from pisa_sample_tools.outcome_eval import evaluate_outcomes

            context.progress("evaluating", message="Evaluating offline outcome conditions")
            result = evaluate_outcomes(
                input_path=input_path,
                config_path=config_path,
                output_dir=output,
                mode=payload.mode,
                default_outcome=payload.default_outcome,
                overwrite=payload.overwrite,
                write_monitor_outcome=payload.write_monitor_outcome,
            )
            context.artifact(result.summary_csv_path, kind="data")
            return result

        return jobs.submit("outcome_eval", payload.model_dump(), run)

    @app.post(f"{API_PREFIX}/tools/repair/scan", tags=["tools"])
    async def repair_scan(payload: RepairScanRequest) -> dict[str, Any]:
        return repairs.scan(payload).model_dump()

    @app.post(
        f"{API_PREFIX}/tools/repair/apply",
        response_model=Job,
        status_code=202,
        tags=["tools"],
    )
    async def repair_apply(payload: RepairApplyRequest) -> Job:
        def run(context: JobContext) -> Any:
            context.progress("verifying", message="Verifying repair plan and source hashes")
            result = repairs.apply(
                payload.plan,
                confirm_path=payload.confirm_path,
                dry_run=payload.dry_run,
            )
            for path in result.get("files", []):
                context.artifact(path, kind="repaired_agent_states")
            return result

        return jobs.submit("repair_apply", payload.model_dump(), run)

    @app.post(
        f"{API_PREFIX}/tools/repair/restore",
        response_model=Job,
        status_code=202,
        tags=["tools"],
    )
    async def repair_restore(payload: RepairRestoreRequest) -> Job:
        policy.resolve(payload.source_path, field="source_path", kind="directory")

        def run(context: JobContext) -> Any:
            context.progress("restoring", message="Restoring verified repair backups")
            return repairs.restore(
                payload.source_path,
                confirm_path=payload.confirm_path,
                backup_suffix=payload.backup_suffix,
                dry_run=payload.dry_run,
            )

        return jobs.submit("repair_restore", payload.model_dump(), run)

    @app.get(f"{API_PREFIX}/jobs", tags=["jobs"])
    async def list_jobs(
        limit: int = Query(default=100, ge=1, le=1_000),
        status: str | None = Query(default=None, pattern="^(queued|running|succeeded|failed|cancelled)$"),
    ) -> dict[str, Any]:
        items = jobs.list(limit=limit, status=status)
        return {"jobs": items, "items": items}

    @app.get(f"{API_PREFIX}/jobs/events", tags=["jobs"])
    async def all_job_events() -> StreamingResponse:
        async def stream():
            sequence = 0
            previous = ""
            while True:
                items = [item.model_dump(mode="json") for item in jobs.list(limit=100)]
                payload = json.dumps(items, sort_keys=True, separators=(",", ":"))
                if payload != previous:
                    sequence += 1
                    previous = payload
                    yield f"id: {sequence}\nevent: progress\ndata: {payload}\n\n"
                else:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(1.0)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get(f"{API_PREFIX}/jobs/{{job_id}}", response_model=Job, tags=["jobs"])
    async def get_job(job_id: str) -> Job:
        try:
            return jobs.get(job_id)
        except KeyError as exc:
            raise APIError(404, "job_not_found", "job was not found") from exc

    @app.post(f"{API_PREFIX}/jobs/{{job_id}}/cancel", response_model=Job, tags=["jobs"])
    async def cancel_job(job_id: str) -> Job:
        try:
            return jobs.cancel(job_id)
        except KeyError as exc:
            raise APIError(404, "job_not_found", "job was not found") from exc

    @app.get(f"{API_PREFIX}/jobs/{{job_id}}/events", tags=["jobs"])
    async def job_events(
        job_id: str,
        after: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        try:
            jobs.get(job_id)
        except KeyError as exc:
            raise APIError(404, "job_not_found", "job was not found") from exc
        if last_event_id:
            try:
                after = max(after, int(last_event_id))
            except ValueError as exc:
                raise APIError(
                    400, "invalid_last_event_id", "Last-Event-ID must be an integer"
                ) from exc

        async def stream():
            cursor = after
            while True:
                events = jobs.events(job_id, after=cursor)
                for event in events:
                    cursor = event.sequence
                    yield (
                        f"id: {event.sequence}\n"
                        f"event: {event.type}\n"
                        f"data: {event.model_dump_json()}\n\n"
                    )
                job = jobs.get(job_id)
                if job.status in TERMINAL_STATES and not jobs.events(job_id, after=cursor):
                    return
                if not events:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/{frontend_path:path}", include_in_schema=False, response_model=None)
    async def frontend(frontend_path: str):
        if frontend_path.startswith("api/"):
            raise APIError(404, "route_not_found", "API route was not found")
        if selected_frontend is None:
            return HTMLResponse(
                "<!doctype html><title>PISA Analysis Workbench</title>"
                "<h1>PISA Analysis Workbench</h1>"
                "<p>The frontend bundle is not installed. API documentation is available at "
                '<a href="/docs">/docs</a>.</p>'
            )
        if not frontend_path:
            return RedirectResponse("/ui/", status_code=307)
        asset_path = frontend_path.removeprefix("ui/")
        requested = (selected_frontend / asset_path).resolve()
        if (
            frontend_path
            and requested.is_relative_to(selected_frontend)
            and requested.is_file()
            and requested.name != "index.html"
        ):
            return Response(
                requested.read_bytes(),
                media_type=mimetypes.guess_type(requested.name)[0] or "application/octet-stream",
            )
        return HTMLResponse((selected_frontend / "index.html").read_text(encoding="utf-8"))

    return app


def _error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    *,
    field: str | None = None,
    details: Any | None = None,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    body = ErrorBody(
        code=code,
        message=message,
        field=field,
        details=details,
        request_id=request_id,
    )
    return JSONResponse(status_code=status_code, content=jsonable_encoder(body))


def _normalize_roots(value: Sequence[Path] | None) -> list[Path]:
    if value is None:
        return []
    return [Path(item).expanduser().resolve() for item in value]


def _deduplicate(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved not in result:
            result.append(resolved)
    return result


def _optional_file(policy: PathPolicy, value: str | None, field: str) -> Path | None:
    return policy.resolve(value, field=field, kind="file") if value else None


def _optional_path(policy: PathPolicy, value: str | None, field: str) -> Path | None:
    return policy.resolve(value, field=field) if value else None


def _validate_campaign_paths(path: Path, policy: PathPolicy) -> None:
    try:
        from pisa_sample_tools.evidence.campaign import load_campaign

        datasets = load_campaign(path)
    except (OSError, ValueError) as exc:
        raise APIError(400, "campaign_invalid", str(exc), field="campaign_path") from exc
    for index, dataset in enumerate(datasets):
        policy.resolve(
            dataset.results_path,
            field=f"campaign.datasets.{index}.results",
            kind="directory",
        )


def _runner_services(
    *,
    config: Path | None,
    local_config: Path | None,
    store: Any | None,
    manager: Any | None,
) -> tuple[Any | None, Any | None]:
    try:
        from pisa_sample_tools.experiment_runner.config import ConfigStore
        from pisa_sample_tools.experiment_runner.orchestrator import JobManager as RunnerJobManager
    except ImportError:
        return store, manager
    if store is None:
        config_path = config or (Path.home() / ".config" / "pisa-analysis-tools" / "experiments.yaml")
        store = ConfigStore(config_path, local_config)
    if manager is None:
        manager = RunnerJobManager()
    return store, manager


def _require_runner(value: Any | None) -> Any:
    if value is None:
        raise APIError(501, "experiment_runner_unavailable", "experiment runner is unavailable")
    return value


def _canonical_runner_job(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    if result.get("status") == "report_ready":
        result["status"] = "succeeded"
        result["phase"] = "report_ready"
    result["id"] = result.pop("job_id", None)
    return result


def _find_frontend(explicit: Path | None) -> Path | None:
    candidates = []
    if explicit is not None:
        candidates.append(Path(explicit).expanduser())
    candidates.extend(
        [
            Path(__file__).with_name("static"),
            Path(__file__).resolve().parents[3] / "frontend" / "dist",
        ]
    )
    for candidate in candidates:
        resolved = candidate.resolve()
        if (resolved / "index.html").is_file():
            return resolved
    return None


def _capabilities(
    *, frontend: Path | None, runner_store: Any | None, roots: Sequence[Path]
) -> dict[str, Any]:
    try:
        import pisa_sample_tools.evidence.service  # noqa: F401

        reporting = True
    except ImportError:
        reporting = False
    return {
        "api_version": API_VERSION,
        "frontend": {"available": frontend is not None},
        "samples": {"preview": True, "export": True, "analyze": True},
        "reports": {"available": reporting, "legacy": True, "recursive_library": True},
        "experiments": {"available": runner_store is not None},
        "tools": {
            "trajectory": True,
            "trajectory_compare": True,
            "outcome_eval": True,
            "repair": True,
        },
        "media": {
            "ffmpeg": shutil.which("ffmpeg") is not None,
            "ffprobe": shutil.which("ffprobe") is not None,
            "derived_label": "reconstructed schematic",
        },
        "exports": ["svg", "pdf", "png", "csv", "json", "mp4", "webm", "gif"],
        "allowed_roots": [str(root) for root in roots],
    }
