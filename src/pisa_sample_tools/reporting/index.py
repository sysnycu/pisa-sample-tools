from __future__ import annotations

import base64
import csv
import hashlib
import io
import itertools
import json
import math
import os
import re
import sqlite3
import tempfile
import time
import zlib
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .comparison import classify_comparison, semantic_projection
from .discovery import (
    ExperimentSource,
    discover_experiments,
    discovery_fingerprint,
)
from .models import (
    DataHealthFinding,
    DatasetDescriptor,
    DatasetRelation,
    FindingSeverity,
    IndexBuildResult,
    IndexedAttempt,
    IndexedRun,
    OutcomeSummary,
    RunFilter,
    RunPage,
    SourceFingerprint,
    StageTiming,
)

REPORT_INDEX_SCHEMA_VERSION = 1
REPORT_INDEX_BUILD_VERSION = 2

_CORE_RUN_FIELDS = frozenset(
    {
        "run.status",
        "run.test_outcome",
        "run.stop_condition",
        "run.stop_reason",
        "run.params",
        "run.sample_id",
        "run.concrete_scenario_id",
        "run.attempt",
        "run.parameter_hash",
    }
)
_SUCCESS_OUTCOMES = frozenset({"success", "passed", "pass"})
_FAIL_OUTCOMES = frozenset({"fail", "failure", "failed", "test_fail", "collision"})
_INVALID_OUTCOMES = frozenset({"invalid"})
_SORT_COLUMNS = {
    "run_id": "r.run_id",
    "dataset_id": "r.dataset_id",
    "scenario_id": (
        "CASE WHEN r.scenario_id GLOB '[0-9]*' "
        "AND r.scenario_id NOT GLOB '*[^0-9]*' "
        "THEN '0:' || printf('%020d', CAST(r.scenario_id AS INTEGER)) "
        "ELSE '1:' || r.scenario_id END"
    ),
    "attempt": "r.attempt",
    "sample_id": "r.sample_id",
    "parameter_hash": "r.parameter_hash",
    "status": "r.status",
    "outcome": "r.outcome",
    "outcome_class": "r.outcome_class",
    "duration": "COALESCE((SELECT CASE WHEN m.name IN ('run.final_sim_time_ms','final_sim_time_ms','run.wall_time_ms') THEN m.value_real / 1000.0 ELSE m.value_real END FROM metrics m WHERE m.run_id = r.run_id AND m.name IN ('run.final_sim_time_ms','duration_seconds','sim_duration_s','simulation_time_seconds','simulation_time_s','final_sim_time_ms','run.wall_time_ms','wall_time_seconds') ORDER BY CASE m.name WHEN 'run.final_sim_time_ms' THEN 0 WHEN 'duration_seconds' THEN 1 WHEN 'sim_duration_s' THEN 2 WHEN 'simulation_time_seconds' THEN 3 WHEN 'simulation_time_s' THEN 4 WHEN 'final_sim_time_ms' THEN 5 ELSE 6 END LIMIT 1), 0)",
}


class ReportIndexError(ValueError):
    """Raised when a report index is invalid or incompatible."""


class _TimingCollector:
    def __init__(self) -> None:
        self.items: list[StageTiming] = []

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        start_wall = datetime.now(UTC)
        start = time.perf_counter()
        try:
            yield
        finally:
            finish = datetime.now(UTC)
            self.items.append(
                StageTiming(
                    stage=name,
                    started_at=start_wall.isoformat(),
                    finished_at=finish.isoformat(),
                    duration_seconds=time.perf_counter() - start,
                )
            )


def build_report_index(
    source_roots: Path | Sequence[Path],
    database_path: Path,
    *,
    force: bool = False,
    progress: Callable[[str, float, float, str], None] | None = None,
) -> IndexBuildResult:
    """Build an atomic normalized index, or return a fingerprint-verified cache hit."""

    roots = _normalize_roots(source_roots)
    database_path = database_path.expanduser().resolve()
    timings = _TimingCollector()
    notify = progress or (lambda _stage, _current, _total, _message: None)
    notify("discover", 1, 8, "Discovering experiment outputs")
    with timings.stage("discover"):
        sources = discover_experiments(roots)
    notify("fingerprint", 2, 8, f"Fingerprinting {len(sources)} experiments")
    with timings.stage("fingerprint"):
        fingerprint = discovery_fingerprint(roots, sources)
    with timings.stage("cache_check"):
        cached = None if force else _cached_counts(database_path, fingerprint)
    if cached is not None:
        notify("complete", 8, 8, "Verified the existing report index")
        return IndexBuildResult(
            database_path=database_path,
            source_roots=roots,
            source_fingerprint=fingerprint,
            rebuilt=False,
            dataset_count=cached[0],
            run_count=cached[1],
            attempt_count=cached[2],
            finding_count=cached[3],
            timings=tuple(timings.items),
        )

    database_path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{database_path.name}.", suffix=".building", dir=database_path.parent
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    try:
        notify("schema", 3, 8, "Creating the normalized report schema")
        with timings.stage("schema"):
            connection = sqlite3.connect(temporary_path)
            connection.row_factory = sqlite3.Row
            _configure_connection(connection)
            _create_schema(connection)
        try:
            notify("index", 4, 8, f"Indexing {len(sources)} experiments and their runs")
            with timings.stage("index"):
                findings = _index_sources(
                    connection,
                    sources,
                    progress=lambda index, source: notify(
                        "index",
                        4 + index / max(1, len(sources)),
                        8,
                        f"Indexed experiment {index} / {len(sources)} · {source.dataset_id}",
                    ),
                )
            notify("data_health", 5, 8, "Computing cross-experiment data-health findings")
            with timings.stage("data_health"):
                findings.extend(_cross_dataset_health(connection))
                _insert_findings(connection, findings)
            notify("verify_source", 6, 8, "Verifying that source data did not change")
            with timings.stage("verify_source"):
                refreshed_sources = discover_experiments(roots)
                refreshed_fingerprint = discovery_fingerprint(roots, refreshed_sources)
                if refreshed_fingerprint != fingerprint:
                    raise ReportIndexError(
                        "report sources changed while the index was being built; "
                        "no mixed-source index was published, so retry after source writes finish"
                    )
            notify("finalize", 7, 8, "Optimizing and publishing the report index")
            with timings.stage("finalize"):
                counts = _database_counts(connection)
                now = datetime.now(UTC).isoformat()
                metadata = {
                    "schema_version": str(REPORT_INDEX_SCHEMA_VERSION),
                    "build_version": str(REPORT_INDEX_BUILD_VERSION),
                    "source_fingerprint": fingerprint,
                    "source_roots": _json([str(path) for path in roots]),
                    "built_at": now,
                    "dataset_count": str(counts[0]),
                    "run_count": str(counts[1]),
                    "attempt_count": str(counts[2]),
                    "finding_count": str(counts[3]),
                }
                connection.executemany(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
                    metadata.items(),
                )
                connection.execute("PRAGMA optimize")
                connection.commit()
        finally:
            connection.close()
        os.replace(temporary_path, database_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise

    notify("complete", 8, 8, "Normalized report index is ready")
    return IndexBuildResult(
        database_path=database_path,
        source_roots=roots,
        source_fingerprint=fingerprint,
        rebuilt=True,
        dataset_count=counts[0],
        run_count=counts[1],
        attempt_count=counts[2],
        finding_count=counts[3],
        timings=tuple(timings.items),
    )


class ReportIndex:
    """Read-only query facade for a normalized report index."""

    def __init__(self, database_path: Path):
        self.database_path = database_path.expanduser().resolve()
        uri = f"file:{self.database_path.as_posix()}?mode=ro"
        self._connection = sqlite3.connect(uri, uri=True)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA query_only=ON")
        metadata = self.metadata()
        try:
            schema_version = int(metadata["schema_version"])
        except (KeyError, ValueError) as exc:
            self.close()
            raise ReportIndexError("database is not a versioned PISA report index") from exc
        if schema_version < REPORT_INDEX_SCHEMA_VERSION:
            self.close()
            raise ReportIndexError(
                f"report index schema {schema_version} is older than supported "
                f"schema {REPORT_INDEX_SCHEMA_VERSION}"
            )
        self.schema_version = schema_version
        self.is_newer_schema = schema_version > REPORT_INDEX_SCHEMA_VERSION

    def __enter__(self) -> ReportIndex:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def metadata(self) -> dict[str, str]:
        return {
            str(row["key"]): str(row["value"])
            for row in self._connection.execute("SELECT key, value FROM metadata")
        }

    def datasets(self) -> tuple[DatasetDescriptor, ...]:
        rows = self._connection.execute(
            """
            SELECT d.*,
                   SUM(CASE WHEN f.severity = 'error' THEN 1 ELSE 0 END) AS health_error,
                   SUM(CASE WHEN f.severity = 'warning' THEN 1 ELSE 0 END) AS health_warning,
                   SUM(CASE WHEN f.severity = 'info' THEN 1 ELSE 0 END) AS health_info
            FROM datasets d
            LEFT JOIN findings f ON f.dataset_id = d.dataset_id
            GROUP BY d.dataset_id
            ORDER BY d.dataset_id
            """
        ).fetchall()
        return tuple(_dataset_from_row(row) for row in rows)

    def dataset(self, dataset_id: str) -> DatasetDescriptor | None:
        row = self._connection.execute(
            """
            SELECT d.*,
                   SUM(CASE WHEN f.severity = 'error' THEN 1 ELSE 0 END) AS health_error,
                   SUM(CASE WHEN f.severity = 'warning' THEN 1 ELSE 0 END) AS health_warning,
                   SUM(CASE WHEN f.severity = 'info' THEN 1 ELSE 0 END) AS health_info
            FROM datasets d
            LEFT JOIN findings f ON f.dataset_id = d.dataset_id
            WHERE d.dataset_id = ?
            GROUP BY d.dataset_id
            """,
            (dataset_id,),
        ).fetchone()
        return _dataset_from_row(row) if row is not None else None

    def dataset_relations(self) -> tuple[DatasetRelation, ...]:
        rows = self._connection.execute(
            """
            SELECT left_dataset_id, right_dataset_id, role, details_json
            FROM dataset_relations
            ORDER BY role, left_dataset_id, right_dataset_id
            """
        ).fetchall()
        return tuple(
            DatasetRelation(
                left_dataset_id=str(row["left_dataset_id"]),
                right_dataset_id=str(row["right_dataset_id"]),
                role=str(row["role"]),
                details=_load_json(row["details_json"], {}),
            )
            for row in rows
        )

    def findings(
        self,
        *,
        dataset_id: str | None = None,
        severity: FindingSeverity | str | None = None,
    ) -> tuple[DataHealthFinding, ...]:
        conditions: list[str] = []
        arguments: list[Any] = []
        if dataset_id is not None:
            conditions.append("dataset_id = ?")
            arguments.append(dataset_id)
        if severity is not None:
            conditions.append("severity = ?")
            arguments.append(str(severity))
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self._connection.execute(
            f"SELECT * FROM findings {where} ORDER BY severity, code, finding_id", arguments
        ).fetchall()
        return tuple(
            DataHealthFinding(
                code=str(row["code"]),
                severity=FindingSeverity(row["severity"]),
                message=str(row["message"]),
                dataset_id=row["dataset_id"],
                run_id=row["run_id"],
                details=_load_json(row["details_json"], {}),
            )
            for row in rows
        )

    def source_fingerprints(
        self, *, dataset_id: str | None = None
    ) -> tuple[SourceFingerprint, ...]:
        if dataset_id is None:
            rows = self._connection.execute(
                "SELECT * FROM source_fingerprints ORDER BY dataset_id, path"
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM source_fingerprints WHERE dataset_id = ? ORDER BY path",
                (dataset_id,),
            ).fetchall()
        return tuple(
            SourceFingerprint(
                path=Path(row["path"]),
                kind=str(row["kind"]),
                size=row["size"],
                mtime_ns=row["mtime_ns"],
                sha256=row["sha256"],
                expected_sha256=row["expected_sha256"],
                status=str(row["status"]),
            )
            for row in rows
        )

    def outcome_summary(self, *, dataset_id: str | None = None) -> OutcomeSummary:
        where = "WHERE dataset_id = ?" if dataset_id is not None else ""
        arguments = (dataset_id,) if dataset_id is not None else ()
        row = self._connection.execute(
            f"""
            SELECT COUNT(*) AS total,
                   SUM(outcome_class = 'success') AS success,
                   SUM(outcome_class = 'fail') AS fail,
                   SUM(outcome_class = 'invalid') AS invalid,
                   SUM(outcome_class = 'unknown') AS unknown,
                   SUM(has_collision = 1) AS collision
            FROM runs {where}
            """,
            arguments,
        ).fetchone()
        return OutcomeSummary(
            total=int(row["total"] or 0),
            success=int(row["success"] or 0),
            fail=int(row["fail"] or 0),
            invalid=int(row["invalid"] or 0),
            unknown=int(row["unknown"] or 0),
            collision=int(row["collision"] or 0),
        )

    def run(self, run_id: str) -> IndexedRun | None:
        row = self._connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return _run_from_row(row, self._connection) if row is not None else None

    def attempts(self, run_id: str) -> tuple[IndexedAttempt, ...]:
        run = self._connection.execute(
            "SELECT dataset_id, scenario_id FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run is None:
            return ()
        rows = self._connection.execute(
            """
            SELECT * FROM attempts
            WHERE dataset_id = ? AND scenario_id = ?
            ORDER BY attempt, row_index
            """,
            (run["dataset_id"], run["scenario_id"]),
        ).fetchall()
        return tuple(_attempt_from_row(row) for row in rows)

    def page_runs(
        self,
        *,
        filters: RunFilter | None = None,
        limit: int = 100,
        cursor: str | None = None,
        sort_by: str = "scenario_id",
        sort_direction: str = "asc",
    ) -> RunPage:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        metric_sort = sort_by.startswith("metric:") and bool(
            re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", sort_by[7:])
        )
        if sort_by not in _SORT_COLUMNS and not metric_sort:
            raise ValueError(f"unsupported run sort: {sort_by}")
        direction = sort_direction.lower()
        if direction not in {"asc", "desc"}:
            raise ValueError("sort_direction must be 'asc' or 'desc'")
        filters = filters or RunFilter()
        conditions, arguments = _run_conditions(filters)
        total_where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        total = int(
            self._connection.execute(
                f"SELECT COUNT(*) FROM runs r {total_where}", arguments
            ).fetchone()[0]
        )

        column = (
            f"(SELECT m.value_real FROM metrics m WHERE m.run_id = r.run_id AND m.name = '{sort_by[7:]}' LIMIT 1)"
            if metric_sort
            else _SORT_COLUMNS[sort_by]
        )
        sort_expression = column if sort_by == "attempt" else f"COALESCE({column}, '')"
        page_conditions = list(conditions)
        page_arguments = list(arguments)
        if cursor is not None:
            cursor_value, cursor_id, cursor_sort, cursor_direction = _decode_cursor(cursor)
            if cursor_sort != sort_by or cursor_direction != direction:
                raise ValueError("cursor does not match the requested sort")
            operator = ">" if direction == "asc" else "<"
            page_conditions.append(
                f"({sort_expression} {operator} ? OR "
                f"({sort_expression} = ? AND r.run_id {operator} ?))"
            )
            page_arguments.extend((cursor_value, cursor_value, cursor_id))
        page_where = f"WHERE {' AND '.join(page_conditions)}" if page_conditions else ""
        rows = self._connection.execute(
            f"""
            SELECT r.*, {sort_expression} AS _sort_value FROM runs r
            {page_where}
            ORDER BY {sort_expression} {direction.upper()}, r.run_id {direction.upper()}
            LIMIT ?
            """,
            (*page_arguments, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = tuple(_run_from_row(row, self._connection) for row in rows)
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            value = last["_sort_value"]
            if value is None and sort_by != "attempt":
                value = ""
            next_cursor = _encode_cursor(value, str(last["run_id"]), sort_by, direction)
        return RunPage(items=items, total=total, limit=limit, next_cursor=next_cursor)


def _normalize_roots(source_roots: Path | Sequence[Path]) -> tuple[Path, ...]:
    roots = (source_roots,) if isinstance(source_roots, Path) else tuple(source_roots)
    if not roots:
        raise ValueError("at least one report source root is required")
    return tuple(sorted({Path(path).expanduser().resolve() for path in roots}, key=str))


def _cached_counts(database_path: Path, fingerprint: str) -> tuple[int, int, int, int] | None:
    if not database_path.is_file():
        return None
    try:
        uri = f"file:{database_path.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            values = dict(connection.execute("SELECT key, value FROM metadata"))
    except sqlite3.Error:
        return None
    if (
        values.get("source_fingerprint") != fingerprint
        or values.get("schema_version") != str(REPORT_INDEX_SCHEMA_VERSION)
        or values.get("build_version") != str(REPORT_INDEX_BUILD_VERSION)
    ):
        return None
    try:
        return tuple(
            int(values[key])
            for key in ("dataset_count", "run_count", "attempt_count", "finding_count")
        )  # type: ignore[return-value]
    except KeyError, ValueError:
        return None


def _configure_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA page_size=8192")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA temp_store=MEMORY")
    connection.execute("PRAGMA cache_size=-65536")


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE datasets (
            dataset_id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL UNIQUE,
            manifest_path TEXT,
            execution_id TEXT,
            scenario_name TEXT,
            simulator TEXT,
            av TEXT,
            sampler TEXT,
            completed_at TEXT,
            expected_runs INTEGER,
            run_count INTEGER NOT NULL DEFAULT 0,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            canonical_digest TEXT NOT NULL DEFAULT '',
            source_fingerprint TEXT NOT NULL DEFAULT '',
            manifest_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE attempts (
            attempt_id INTEGER PRIMARY KEY,
            dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
            scenario_id TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            row_index INTEGER NOT NULL,
            result_path TEXT NOT NULL,
            sample_id TEXT,
            parameter_hash TEXT,
            status TEXT,
            outcome TEXT,
            stop_condition TEXT,
            stop_reason TEXT,
            row_data BLOB NOT NULL,
            row_digest TEXT NOT NULL
        );
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
            scenario_id TEXT NOT NULL,
            canonical_attempt_id INTEGER NOT NULL REFERENCES attempts(attempt_id),
            attempt INTEGER NOT NULL,
            sample_id TEXT,
            parameter_hash TEXT,
            params_json TEXT NOT NULL,
            status TEXT,
            outcome TEXT,
            outcome_class TEXT NOT NULL,
            stop_condition TEXT,
            stop_reason TEXT,
            result_path TEXT NOT NULL,
            trace_paths_json TEXT NOT NULL,
            provenance_signature TEXT,
            row_digest TEXT NOT NULL,
            has_collision INTEGER NOT NULL DEFAULT 0,
            UNIQUE(dataset_id, scenario_id)
        );
        CREATE TABLE parameters (
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            value_text TEXT,
            value_real REAL,
            value_type TEXT NOT NULL,
            PRIMARY KEY(run_id, name)
        ) WITHOUT ROWID;
        CREATE TABLE metrics (
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            value_text TEXT,
            value_real REAL,
            value_type TEXT NOT NULL,
            PRIMARY KEY(run_id, name)
        ) WITHOUT ROWID;
        CREATE TABLE source_fingerprints (
            fingerprint_id INTEGER PRIMARY KEY,
            dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
            path TEXT NOT NULL,
            kind TEXT NOT NULL,
            size INTEGER,
            mtime_ns INTEGER,
            sha256 TEXT,
            expected_sha256 TEXT,
            status TEXT NOT NULL,
            UNIQUE(dataset_id, path, kind)
        );
        CREATE TABLE findings (
            finding_id INTEGER PRIMARY KEY,
            code TEXT NOT NULL,
            severity TEXT NOT NULL,
            message TEXT NOT NULL,
            dataset_id TEXT REFERENCES datasets(dataset_id) ON DELETE CASCADE,
            run_id TEXT REFERENCES runs(run_id) ON DELETE CASCADE,
            details_json TEXT NOT NULL
        );
        CREATE TABLE dataset_relations (
            left_dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
            right_dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            details_json TEXT NOT NULL,
            PRIMARY KEY(left_dataset_id, right_dataset_id)
        ) WITHOUT ROWID;

        CREATE INDEX attempts_dataset_scenario ON attempts(dataset_id, scenario_id, attempt);
        CREATE INDEX runs_dataset_scenario ON runs(dataset_id, scenario_id, run_id);
        CREATE INDEX runs_outcome ON runs(outcome_class, outcome);
        CREATE INDEX runs_status ON runs(status);
        CREATE INDEX runs_parameter_hash ON runs(parameter_hash);
        CREATE INDEX parameters_name_real ON parameters(name, value_real);
        CREATE INDEX findings_dataset ON findings(dataset_id, severity, code);
        """
    )


def _index_sources(
    connection: sqlite3.Connection,
    sources: Sequence[ExperimentSource],
    *,
    progress: Callable[[int, ExperimentSource], None] | None = None,
) -> list[DataHealthFinding]:
    findings: list[DataHealthFinding] = []
    for source_index, source in enumerate(sources, start=1):
        manifest, manifest_bytes = _load_manifest(source.manifest_path)
        descriptor = _manifest_descriptor(manifest)
        expected_runs = _expected_run_count(manifest, source.root)
        connection.execute(
            """
            INSERT INTO datasets(
                dataset_id, source_path, manifest_path, execution_id, scenario_name,
                simulator, av, sampler, completed_at, expected_runs, manifest_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source.dataset_id,
                str(source.root),
                str(source.manifest_path) if source.manifest_path else None,
                descriptor["execution_id"],
                descriptor["scenario_name"],
                descriptor["simulator"],
                descriptor["av"],
                descriptor["sampler"],
                descriptor["completed_at"],
                expected_runs,
                _json(manifest),
            ),
        )
        source_hash = hashlib.sha256()
        if source.manifest_path is None:
            findings.append(
                DataHealthFinding(
                    code="missing_manifest",
                    severity=FindingSeverity.WARNING,
                    message="No execution manifest was found; provenance is incomplete.",
                    dataset_id=source.dataset_id,
                )
            )
        else:
            manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
            source_hash.update(manifest_sha.encode())
            _insert_source_fingerprint(
                connection,
                source.dataset_id,
                SourceFingerprint(
                    path=source.manifest_path,
                    kind="execution_manifest",
                    size=len(manifest_bytes),
                    mtime_ns=source.manifest_path.stat().st_mtime_ns,
                    sha256=manifest_sha,
                    status="verified",
                ),
            )

        findings.extend(_resolved_input_findings(connection, source.dataset_id, manifest))
        if source.missing_result_dirs:
            findings.append(
                DataHealthFinding(
                    code="missing_results",
                    severity=FindingSeverity.WARNING,
                    message=(
                        f"{len(source.missing_result_dirs)} iteration director"
                        f"{'y is' if len(source.missing_result_dirs) == 1 else 'ies are'} "
                        "missing monitor/result.csv."
                    ),
                    dataset_id=source.dataset_id,
                    details={
                        "count": len(source.missing_result_dirs),
                        "examples": [str(path) for path in source.missing_result_dirs[:20]],
                    },
                )
            )

        canonical_rows: list[tuple[str, str, str]] = []
        provenance_counts: Counter[str] = Counter()
        missing_trace_counts: Counter[str] = Counter()
        attempt_count = 0
        run_count = 0
        for result_path in source.result_paths:
            scenario_id = _scenario_id(result_path)
            rows, raw_bytes = _read_result_rows(result_path)
            result_sha = hashlib.sha256(raw_bytes).hexdigest()
            source_hash.update(str(result_path.relative_to(source.root)).encode())
            source_hash.update(result_sha.encode())
            _insert_source_fingerprint(
                connection,
                source.dataset_id,
                SourceFingerprint(
                    path=result_path,
                    kind="result_csv",
                    size=len(raw_bytes),
                    mtime_ns=result_path.stat().st_mtime_ns,
                    sha256=result_sha,
                    status="verified",
                ),
            )
            if not rows:
                findings.append(
                    DataHealthFinding(
                        code="empty_result",
                        severity=FindingSeverity.WARNING,
                        message="monitor/result.csv has no data rows.",
                        dataset_id=source.dataset_id,
                        details={"path": str(result_path)},
                    )
                )
                continue
            parsed = [_parse_result_row(row, row_index) for row_index, row in enumerate(rows)]
            attempt_ids: list[int] = []
            for item in parsed:
                cursor = connection.execute(
                    """
                    INSERT INTO attempts(
                        dataset_id, scenario_id, attempt, row_index, result_path,
                        sample_id, parameter_hash, status, outcome,
                        stop_condition, stop_reason, row_data, row_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source.dataset_id,
                        scenario_id,
                        item["attempt"],
                        item["row_index"],
                        str(result_path),
                        item["sample_id"],
                        item["parameter_hash"],
                        item["status"],
                        item["outcome"],
                        item["stop_condition"],
                        item["stop_reason"],
                        zlib.compress(_json(item).encode(), level=6),
                        item["row_digest"],
                    ),
                )
                attempt_ids.append(int(cursor.lastrowid))
            attempt_count += len(parsed)
            canonical_index = max(
                range(len(parsed)), key=lambda index: (parsed[index]["attempt"], index)
            )
            canonical = parsed[canonical_index]
            canonical_attempt_id = attempt_ids[canonical_index]
            trace_paths, missing_names = _trace_paths(result_path.parent)
            missing_trace_counts.update(missing_names)
            provenance = _geometry_provenance(result_path.parent / "agent_geometry.csv")
            if provenance:
                provenance_counts[provenance] += 1
            has_collision = _csv_has_data(
                result_path.parent / "collision_events.csv"
            ) or _row_collision(canonical)
            run_id = f"{source.dataset_id}:{scenario_id}"
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, dataset_id, scenario_id, canonical_attempt_id, attempt,
                    sample_id, parameter_hash, params_json, status, outcome, outcome_class,
                    stop_condition, stop_reason, result_path, trace_paths_json,
                    provenance_signature, row_digest, has_collision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    source.dataset_id,
                    scenario_id,
                    canonical_attempt_id,
                    canonical["attempt"],
                    canonical["sample_id"],
                    canonical["parameter_hash"],
                    _json(canonical["params"]),
                    canonical["status"],
                    canonical["outcome"],
                    _outcome_class(canonical["outcome"]),
                    canonical["stop_condition"],
                    canonical["stop_reason"],
                    str(result_path),
                    _json(trace_paths),
                    provenance,
                    canonical["row_digest"],
                    int(has_collision),
                ),
            )
            connection.executemany(
                """
                INSERT INTO parameters(run_id, name, value_text, value_real, value_type)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    (run_id, name, *_typed_columns(value))
                    for name, value in canonical["params"].items()
                ),
            )
            connection.executemany(
                """
                INSERT INTO metrics(run_id, name, value_text, value_real, value_type)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    (run_id, name, *_typed_columns(value))
                    for name, value in {
                        **_control_summary_metrics(trace_paths),
                        **canonical["metrics"],
                    }.items()
                ),
            )
            canonical_rows.append(
                (
                    scenario_id,
                    canonical["parameter_hash"] or "",
                    canonical["row_digest"],
                )
            )
            run_count += 1

        canonical_digest = _canonical_dataset_digest(canonical_rows)
        connection.execute(
            """
            UPDATE datasets
            SET run_count = ?, attempt_count = ?, canonical_digest = ?, source_fingerprint = ?
            WHERE dataset_id = ?
            """,
            (
                run_count,
                attempt_count,
                canonical_digest,
                source_hash.hexdigest(),
                source.dataset_id,
            ),
        )
        if missing_trace_counts:
            findings.append(
                DataHealthFinding(
                    code="missing_trace_files",
                    severity=FindingSeverity.WARNING,
                    message="One or more optional per-run trace streams are missing.",
                    dataset_id=source.dataset_id,
                    details={"counts": dict(sorted(missing_trace_counts.items()))},
                )
            )
        if len(provenance_counts) > 1:
            findings.append(
                DataHealthFinding(
                    code="mixed_provenance",
                    severity=FindingSeverity.ERROR,
                    message="Run-level geometry identifies multiple simulator provenance families.",
                    dataset_id=source.dataset_id,
                    details={"signatures": dict(sorted(provenance_counts.items()))},
                )
            )
        partial_reasons: list[str] = []
        if manifest and not descriptor["completed_at"]:
            partial_reasons.append("manifest completed_at is missing")
        if expected_runs is not None and run_count < expected_runs:
            partial_reasons.append(f"indexed {run_count} of {expected_runs} expected runs")
        if partial_reasons:
            findings.append(
                DataHealthFinding(
                    code="partial_dataset",
                    severity=FindingSeverity.WARNING,
                    message="Dataset appears incomplete: " + "; ".join(partial_reasons) + ".",
                    dataset_id=source.dataset_id,
                    details={"run_count": run_count, "expected_runs": expected_runs},
                )
            )
        if progress is not None:
            progress(source_index, source)
    connection.commit()
    return findings


def _cross_dataset_health(connection: sqlite3.Connection) -> list[DataHealthFinding]:
    findings: list[DataHealthFinding] = []
    duplicate_pairs: set[tuple[str, str]] = set()
    groups = connection.execute(
        """
        SELECT canonical_digest, run_count, GROUP_CONCAT(dataset_id, char(31)) AS dataset_ids
        FROM datasets
        WHERE run_count > 0
        GROUP BY canonical_digest, run_count
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for row in groups:
        dataset_ids = sorted(str(row["dataset_ids"]).split(chr(31)))
        canonical = dataset_ids[0]
        duplicate_pairs.update(itertools.combinations(dataset_ids, 2))
        for alias in dataset_ids[1:]:
            connection.execute(
                """
                INSERT INTO dataset_relations(
                    left_dataset_id, right_dataset_id, role, details_json
                ) VALUES (?, ?, 'duplicate_alias', ?)
                """,
                (
                    canonical,
                    alias,
                    _json(
                        {
                            "canonical_digest": row["canonical_digest"],
                            "run_count": row["run_count"],
                        }
                    ),
                ),
            )
        for dataset_id in dataset_ids:
            findings.append(
                DataHealthFinding(
                    code="duplicate_alias",
                    severity=FindingSeverity.WARNING,
                    message=(
                        "Canonical results are identical to another dataset and should not be "
                        "double-counted."
                    ),
                    dataset_id=dataset_id,
                    details={
                        "datasets": dataset_ids,
                        "canonical_dataset": canonical,
                        "canonical_digest": row["canonical_digest"],
                    },
                )
            )
    _insert_comparison_relations(connection, duplicate_pairs)
    return findings


def _insert_comparison_relations(
    connection: sqlite3.Connection, duplicate_pairs: set[tuple[str, str]]
) -> None:
    datasets = connection.execute(
        "SELECT dataset_id, run_count, manifest_json FROM datasets ORDER BY dataset_id"
    ).fetchall()
    # A hash is a valid pairing key only when it identifies exactly one canonical
    # run in each dataset.  This is deliberately the same contract used by the
    # comparison charts: ambiguous duplicate hashes are never paired arbitrarily.
    hashes: dict[str, set[str]] = {str(row["dataset_id"]): set() for row in datasets}
    for row in connection.execute(
        """
        SELECT dataset_id, parameter_hash
        FROM runs
        WHERE parameter_hash IS NOT NULL AND parameter_hash <> ''
        GROUP BY dataset_id, parameter_hash
        HAVING COUNT(*) = 1
        """
    ):
        hashes[str(row["dataset_id"])].add(str(row["parameter_hash"]))
    parameter_names: dict[str, set[str]] = {
        str(row["dataset_id"]): set() for row in datasets
    }
    for row in connection.execute(
        """
        SELECT r.dataset_id, p.name
        FROM parameters AS p JOIN runs AS r ON r.run_id = p.run_id
        GROUP BY r.dataset_id, p.name
        """
    ):
        parameter_names[str(row["dataset_id"])].add(str(row["name"]))

    for left_row, right_row in itertools.combinations(datasets, 2):
        left = str(left_row["dataset_id"])
        right = str(right_row["dataset_id"])
        if (left, right) in duplicate_pairs:
            continue
        left_count = int(left_row["run_count"] or 0)
        right_count = int(right_row["run_count"] or 0)
        matched = len(hashes[left] & hashes[right])
        common_domain = bool(parameter_names[left]) and parameter_names[left] == parameter_names[right]
        left_manifest = _load_json(left_row["manifest_json"], {})
        right_manifest = _load_json(right_row["manifest_json"], {})
        compatible, differences, system_changed, policy_changed = _pair_semantics(
            left_manifest, right_manifest
        )
        assessment = classify_comparison(
            left_count=left_count,
            right_count=right_count,
            matched_count=matched,
            semantic_compatible=compatible,
            system_changed=system_changed,
            policy_changed=policy_changed,
            replicate=compatible is True and not system_changed and not policy_changed,
            common_parameter_domain=common_domain,
        )
        connection.execute(
            """
            INSERT INTO dataset_relations(
                left_dataset_id, right_dataset_id, role, details_json
            ) VALUES (?, ?, ?, ?)
            """,
            (
                left,
                right,
                str(assessment.role),
                _json(
                    {
                        **assessment.as_dict(),
                        "matched_count": matched,
                        "pairing_key": "parameter_hash unique within each dataset",
                        "common_parameter_domain": common_domain,
                        "shared_parameter_names": sorted(parameter_names[left] & parameter_names[right]),
                        "semantic_differences": differences,
                    }
                ),
            ),
        )


def _pair_semantics(
    left_manifest: dict[str, Any], right_manifest: dict[str, Any]
) -> tuple[bool | None, dict[str, tuple[Any, Any]], bool, bool]:
    left = semantic_projection(left_manifest)
    right = semantic_projection(right_manifest)
    left_resolved = _mapping(left_manifest.get("resolved_input_sha256"))
    right_resolved = _mapping(right_manifest.get("resolved_input_sha256"))
    for key in ("simulator_config", "av_config"):
        left[key] = left_resolved.get(key)
        right[key] = right_resolved.get(key)
    differences = {
        key: (left.get(key), right.get(key))
        for key in sorted(left)
        if left.get(key) != right.get(key)
    }
    required = (
        "scenario",
        "map",
        "stop_conditions",
        "monitor_config",
        "dt",
        "sampler_config",
        "sampler_source",
        "sampler_name",
        "simulator_config",
        "av_config",
        "observation_identity",
        "observation_order",
    )
    components_complete = all(
        _component_identity_is_complete(projection.get(component))
        for projection in (left, right)
        for component in ("simulator", "av")
    )
    if (
        not components_complete
        or any(
            not _semantic_value_is_recorded(left.get(key))
            or not _semantic_value_is_recorded(right.get(key))
            for key in required
        )
    ):
        # Missing identity/configuration makes even an apparent component change
        # unverified.  Do not promote incomplete provenance to an intervention.
        return None, differences, False, False

    system_keys = {"simulator", "simulator_config"}
    policy_keys = {"av", "av_config"}
    intervention_keys = set(differences) & (system_keys | policy_keys)
    other_differences = set(differences) - intervention_keys
    system_changed = bool(intervention_keys & system_keys)
    policy_changed = bool(intervention_keys & policy_keys) and not system_changed
    if other_differences:
        return False, differences, system_changed, policy_changed
    return True, differences, system_changed, policy_changed


def _component_identity_is_complete(value: Any) -> bool:
    return isinstance(value, dict) and all(
        _semantic_value_is_recorded(value.get(key))
        for key in ("wrapper_name", "wrapper_version", "component_name")
    )


def _semantic_value_is_recorded(value: Any) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _insert_findings(connection: sqlite3.Connection, findings: Sequence[DataHealthFinding]) -> None:
    connection.executemany(
        """
        INSERT INTO findings(code, severity, message, dataset_id, run_id, details_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            (
                finding.code,
                str(finding.severity),
                finding.message,
                finding.dataset_id,
                finding.run_id,
                _json(finding.details),
            )
            for finding in findings
        ),
    )
    connection.commit()


def _database_counts(connection: sqlite3.Connection) -> tuple[int, int, int, int]:
    return tuple(
        int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("datasets", "runs", "attempts", "findings")
    )  # type: ignore[return-value]


def _load_manifest(path: Path | None) -> tuple[dict[str, Any], bytes]:
    if path is None:
        return {}, b""
    try:
        raw = path.read_bytes()
        parsed = json.loads(raw) if path.suffix.lower() == ".json" else yaml.safe_load(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ReportIndexError(f"failed to read execution manifest {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ReportIndexError(f"execution manifest must contain a mapping: {path}")
    return dict(parsed), raw


def _manifest_descriptor(manifest: dict[str, Any]) -> dict[str, Any]:
    components = _mapping(manifest.get("components"))
    execution = _mapping(manifest.get("execution"))
    return {
        "execution_id": _optional_string(manifest.get("execution_id")),
        "scenario_name": _optional_string(manifest.get("scenario_name")),
        "simulator": _component_name(components.get("simulator")),
        "av": _component_name(components.get("av")),
        "sampler": _optional_string(execution.get("sampler_name")),
        "completed_at": _optional_string(manifest.get("completed_at")),
    }


def _component_name(value: Any) -> str | None:
    descriptor = _mapping(value)
    component = _mapping(descriptor.get("component"))
    return _optional_string(component.get("name"))


def _expected_run_count(manifest: dict[str, Any], root: Path) -> int | None:
    summary = _mapping(manifest.get("summary"))
    summary_count = sum(
        _nonnegative_int(summary.get(key)) or 0
        for key in ("finished", "failed", "skipped", "aborted")
    )
    if summary_count:
        return summary_count
    resolved = _mapping(manifest.get("resolved_inputs"))
    candidates = [root.name, str(resolved.get("sampler_config") or "")]
    for candidate in candidates:
        matches = re.findall(r"(?:lhs|grid|sobol|random|feedback|fb)[_-]?(\d+)", candidate.lower())
        if matches:
            return int(matches[-1])
    return None


def _read_result_rows(path: Path) -> tuple[list[dict[str, str]], bytes]:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ReportIndexError(f"failed to read result CSV {path}: {exc}") from exc
    reader = csv.DictReader(io.StringIO(text), skipinitialspace=True)
    rows: list[dict[str, str]] = []
    for raw_row in reader:
        row = {
            (key or "").strip(): value.strip() if isinstance(value, str) else ""
            for key, value in raw_row.items()
            if key is not None
        }
        if any(value for value in row.values()):
            rows.append(row)
    return rows, raw


def _parse_result_row(row: dict[str, str], row_index: int) -> dict[str, Any]:
    params = _json_mapping(row.get("run.params"))
    metrics = {
        key: _coerce_scalar(value)
        for key, value in row.items()
        if key and key not in _CORE_RUN_FIELDS and value not in {"", None}
    }
    attempt = _nonnegative_int(row.get("run.attempt"))
    normalized = {
        "attempt": attempt if attempt is not None else 1,
        "row_index": row_index,
        "sample_id": _optional_string(
            row.get("run.sample_id") or row.get("run.concrete_scenario_id")
        ),
        "parameter_hash": _optional_string(row.get("run.parameter_hash")),
        "params": params,
        "status": _optional_string(row.get("run.status")),
        "outcome": _optional_string(row.get("run.test_outcome")),
        "stop_condition": _optional_string(row.get("run.stop_condition")),
        "stop_reason": _optional_string(row.get("run.stop_reason")),
        "metrics": metrics,
    }
    normalized["row_digest"] = hashlib.sha256(_json(normalized).encode()).hexdigest()
    return normalized


def _scenario_id(result_path: Path) -> str:
    iteration = next(
        (parent for parent in result_path.parents if parent.name.startswith("iteration_")), None
    )
    if iteration is None:
        return "concrete"
    return iteration.name.removeprefix("iteration_") or iteration.name


def _trace_paths(monitor: Path) -> tuple[dict[str, str], list[str]]:
    paths: dict[str, str] = {}
    missing: list[str] = []
    # agent_state.csv is a legacy alternative, not an independently required stream.
    logical_names = {
        "frame_metrics": ("frame_metrics.csv",),
        "agent_states": ("agent_states.csv", "agent_state.csv"),
        "agent_geometry": ("agent_geometry.csv",),
        "collision_events": ("collision_events.csv",),
        "scenario_events": ("scenario_events.csv",),
        "control_commands": ("control_commands.csv",),
    }
    for logical, names in logical_names.items():
        selected = next((monitor / name for name in names if (monitor / name).is_file()), None)
        if selected is None:
            missing.append(logical)
        else:
            paths[logical] = str(selected)
    return paths, missing


def _control_summary_metrics(trace_paths: dict[str, str]) -> dict[str, float]:
    """Derive compact per-run control extrema for scatter exploration."""

    raw_path = trace_paths.get("control_commands") or trace_paths.get("frame_metrics")
    if not raw_path:
        return {}
    values: dict[str, list[float]] = {"throttle": [], "brake": [], "steer": []}
    try:
        with Path(raw_path).open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                for name, raw_value in row.items():
                    lowered = str(name).casefold().replace("-", "_")
                    control = next(
                        (
                            key
                            for key, patterns in {
                                "throttle": ("throttle", "accelerator"),
                                "brake": ("brake",),
                                "steer": ("steer", "steering"),
                            }.items()
                            if any(token in lowered for token in patterns)
                        ),
                        None,
                    )
                    if control is None or raw_value in {None, ""}:
                        continue
                    try:
                        numeric = float(raw_value)
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(numeric):
                        values[control].append(numeric)
    except OSError:
        return {}
    result: dict[str, float] = {}
    if values["throttle"]:
        result["control.max_throttle"] = max(values["throttle"])
    if values["brake"]:
        result["control.max_brake"] = max(values["brake"])
    if values["steer"]:
        result["control.max_abs_steer"] = max(abs(value) for value in values["steer"])
    return result


def _geometry_provenance(path: Path) -> str | None:
    row = _first_csv_row(path)
    if not row:
        return None
    reference = str(row.get("reference_point") or "").strip().lower()
    source = str(row.get("source") or "").strip().lower()
    if "esmini" in reference or "esmini" in source:
        return "esmini"
    if "carla" in reference or "carla" in source:
        return "carla"
    if reference or source:
        return f"{reference or 'unknown'}|{source or 'unknown'}"
    return None


def _first_csv_row(path: Path) -> dict[str, str] | None:
    if not path.is_file():
        return None
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle, skipinitialspace=True)
            row = next(
                (
                    candidate
                    for candidate in reader
                    if any(
                        isinstance(value, str) and value.strip()
                        for key, value in candidate.items()
                        if key is not None
                    )
                ),
                None,
            )
    except OSError, UnicodeDecodeError, csv.Error:
        return None
    if row is None:
        return None
    return {
        (key or "").strip(): value.strip() if isinstance(value, str) else ""
        for key, value in row.items()
        if key is not None
    }


def _csv_has_data(path: Path) -> bool:
    return _first_csv_row(path) is not None


def _row_collision(item: dict[str, Any]) -> bool:
    metric = item["metrics"].get("ego_collision.collision")
    if metric is True or str(metric).strip().lower() in {"1", "true", "yes"}:
        return True
    stop = str(item.get("stop_condition") or "").lower()
    return "collision" in stop


def _outcome_class(outcome: str | None) -> str:
    normalized = str(outcome or "").strip().lower()
    if normalized in _SUCCESS_OUTCOMES:
        return "success"
    if normalized in _FAIL_OUTCOMES:
        return "fail"
    if normalized in _INVALID_OUTCOMES:
        return "invalid"
    return "unknown"


def _canonical_dataset_digest(rows: Sequence[tuple[str, str, str]]) -> str:
    digest = hashlib.sha256()
    for scenario_id, parameter_hash, row_digest in sorted(rows):
        digest.update(f"{scenario_id}\0{parameter_hash}\0{row_digest}\n".encode())
    return digest.hexdigest()


def _resolved_input_findings(
    connection: sqlite3.Connection, dataset_id: str, manifest: dict[str, Any]
) -> list[DataHealthFinding]:
    findings: list[DataHealthFinding] = []
    resolved = _mapping(manifest.get("resolved_inputs"))
    expected = _mapping(manifest.get("resolved_input_sha256"))
    for name, raw_path in sorted(resolved.items()):
        if raw_path in {None, ""}:
            continue
        path = Path(str(raw_path)).expanduser()
        expected_hash = _optional_string(expected.get(name))
        if not path.exists():
            _insert_source_fingerprint(
                connection,
                dataset_id,
                SourceFingerprint(
                    path=path,
                    kind=f"resolved_input:{name}",
                    expected_sha256=expected_hash,
                    status="missing",
                ),
            )
            findings.append(
                DataHealthFinding(
                    code="provenance_source_missing",
                    severity=FindingSeverity.WARNING,
                    message=f"Recorded provenance input '{name}' is no longer available.",
                    dataset_id=dataset_id,
                    details={"name": name, "path": str(path), "expected_sha256": expected_hash},
                )
            )
            continue
        stat = path.stat()
        if path.is_file():
            actual_hash = _sha256_file(path)
            status = (
                "drifted"
                if expected_hash is not None and actual_hash.lower() != expected_hash.lower()
                else "verified"
            )
        else:
            # Directory hashes are producer-specific; record a deterministic stat-tree
            # fingerprint without claiming it is comparable to the producer digest.
            actual_hash = _directory_stat_fingerprint(path)
            status = "recorded"
        _insert_source_fingerprint(
            connection,
            dataset_id,
            SourceFingerprint(
                path=path,
                kind=f"resolved_input:{name}",
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                sha256=actual_hash,
                expected_sha256=expected_hash,
                status=status,
            ),
        )
        if status == "drifted":
            findings.append(
                DataHealthFinding(
                    code="provenance_hash_drift",
                    severity=FindingSeverity.WARNING,
                    message=f"Recorded provenance input '{name}' no longer matches its hash.",
                    dataset_id=dataset_id,
                    details={
                        "name": name,
                        "path": str(path),
                        "expected_sha256": expected_hash,
                        "actual_sha256": actual_hash,
                    },
                )
            )
    return findings


def _insert_source_fingerprint(
    connection: sqlite3.Connection, dataset_id: str, fingerprint: SourceFingerprint
) -> None:
    connection.execute(
        """
        INSERT INTO source_fingerprints(
            dataset_id, path, kind, size, mtime_ns, sha256, expected_sha256, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            dataset_id,
            str(fingerprint.path),
            fingerprint.kind,
            fingerprint.size,
            fingerprint.mtime_ns,
            fingerprint.sha256,
            fingerprint.expected_sha256,
            fingerprint.status,
        ),
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _directory_stat_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        entries = sorted(path.rglob("*"), key=lambda item: item.as_posix())
        for entry in entries:
            if not entry.is_file():
                continue
            stat = entry.stat()
            digest.update(
                f"{entry.relative_to(path).as_posix()}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode()
            )
    except OSError:
        return "unavailable"
    return digest.hexdigest()


def _run_conditions(filters: RunFilter) -> tuple[list[str], list[Any]]:
    conditions: list[str] = []
    arguments: list[Any] = []
    for column, values in (
        ("r.dataset_id", filters.dataset_ids),
        ("r.outcome", filters.outcomes),
        ("r.outcome_class", filters.outcome_classes),
        ("r.status", filters.statuses),
    ):
        if values:
            placeholders = ", ".join("?" for _ in values)
            conditions.append(f"{column} IN ({placeholders})")
            arguments.extend(values)
    if filters.parameter_hash is not None:
        conditions.append("r.parameter_hash = ?")
        arguments.append(filters.parameter_hash)
    if filters.search:
        escaped = _escape_like(filters.search.strip())
        conditions.append(
            "(r.scenario_id LIKE ? ESCAPE '\\' OR r.run_id LIKE ? ESCAPE '\\')"
        )
        arguments.extend((f"%{escaped}%",) * 2)
    for index, (name, value) in enumerate(sorted(filters.parameter_values.items())):
        alias = f"p{index}"
        value_text, value_real, value_type = _typed_columns(value)
        if value_real is not None:
            comparison = f"{alias}.value_real = ?"
            argument = value_real
        else:
            comparison = f"{alias}.value_text = ?"
            argument = value_text
        conditions.append(
            f"EXISTS (SELECT 1 FROM parameters {alias} WHERE {alias}.run_id = r.run_id "
            f"AND {alias}.name = ? AND {comparison})"
        )
        arguments.extend((name, argument))
    return conditions, arguments


def _dataset_from_row(row: sqlite3.Row) -> DatasetDescriptor:
    return DatasetDescriptor(
        dataset_id=str(row["dataset_id"]),
        source_path=Path(row["source_path"]),
        manifest_path=Path(row["manifest_path"]) if row["manifest_path"] else None,
        execution_id=row["execution_id"],
        scenario_name=row["scenario_name"],
        simulator=row["simulator"],
        av=row["av"],
        sampler=row["sampler"],
        completed_at=row["completed_at"],
        expected_runs=row["expected_runs"],
        run_count=int(row["run_count"]),
        attempt_count=int(row["attempt_count"]),
        canonical_digest=str(row["canonical_digest"]),
        source_fingerprint=str(row["source_fingerprint"]),
        health_counts={
            "error": int(row["health_error"] or 0),
            "warning": int(row["health_warning"] or 0),
            "info": int(row["health_info"] or 0),
        },
    )


def _run_from_row(row: sqlite3.Row, connection: sqlite3.Connection) -> IndexedRun:
    trace_values = _load_json(row["trace_paths_json"], {})
    return IndexedRun(
        run_id=str(row["run_id"]),
        dataset_id=str(row["dataset_id"]),
        scenario_id=str(row["scenario_id"]),
        attempt=int(row["attempt"]),
        sample_id=row["sample_id"],
        parameter_hash=row["parameter_hash"],
        params=_load_json(row["params_json"], {}),
        status=row["status"],
        outcome=row["outcome"],
        outcome_class=str(row["outcome_class"]),
        stop_condition=row["stop_condition"],
        stop_reason=row["stop_reason"],
        metrics=_normalized_values(connection, "metrics", str(row["run_id"])),
        result_path=Path(row["result_path"]),
        trace_paths={key: Path(value) for key, value in trace_values.items()},
        provenance_signature=row["provenance_signature"],
        has_collision=bool(row["has_collision"]),
    )


def _normalized_values(connection: sqlite3.Connection, table: str, run_id: str) -> dict[str, Any]:
    if table not in {"parameters", "metrics"}:  # pragma: no cover - internal invariant
        raise ValueError(f"unsupported normalized value table: {table}")
    rows = connection.execute(
        f"SELECT name, value_text, value_real, value_type FROM {table} WHERE run_id = ?",
        (run_id,),
    )
    return {
        str(row["name"]): _decode_typed_value(
            row["value_text"], row["value_real"], str(row["value_type"])
        )
        for row in rows
    }


def _decode_typed_value(value_text: str | None, value_real: float | None, value_type: str) -> Any:
    if value_type == "null":
        return None
    if value_type == "boolean":
        return value_text == "true"
    if value_type == "number":
        if value_text is not None and re.fullmatch(r"[-+]?\d+", value_text):
            return int(value_text)
        return float(value_real) if value_real is not None else None
    if value_type == "json":
        return _load_json(value_text or "", None)
    return value_text


def _attempt_from_row(row: sqlite3.Row) -> IndexedAttempt:
    try:
        item = json.loads(zlib.decompress(row["row_data"]))
    except (TypeError, ValueError, zlib.error, json.JSONDecodeError) as exc:
        raise ReportIndexError(
            f"attempt payload is corrupt for {row['dataset_id']}:{row['scenario_id']}"
        ) from exc
    return IndexedAttempt(
        dataset_id=str(row["dataset_id"]),
        scenario_id=str(row["scenario_id"]),
        attempt=int(row["attempt"]),
        row_index=int(row["row_index"]),
        sample_id=item.get("sample_id"),
        parameter_hash=item.get("parameter_hash"),
        params=dict(item.get("params") or {}),
        status=item.get("status"),
        outcome=item.get("outcome"),
        stop_condition=item.get("stop_condition"),
        stop_reason=item.get("stop_reason"),
        metrics=dict(item.get("metrics") or {}),
        result_path=Path(row["result_path"]),
        row_digest=str(row["row_digest"]),
    )


def _encode_cursor(value: Any, run_id: str, sort: str, direction: str) -> str:
    payload = _json({"value": value, "run_id": run_id, "sort": sort, "direction": direction})
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _cursor_sort_value(row: sqlite3.Row, sort_by: str) -> Any:
    value = row[sort_by]
    if sort_by != "scenario_id":
        return value
    scenario_id = str(value)
    return f"0:{int(scenario_id):020d}" if scenario_id.isdigit() else f"1:{scenario_id}"


def _decode_cursor(cursor: str) -> tuple[Any, str, str, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding))
        return payload["value"], payload["run_id"], payload["sort"], payload["direction"]
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid run cursor") from exc


def _typed_columns(value: Any) -> tuple[str | None, float | None, str]:
    if value is None:
        return None, None, "null"
    if isinstance(value, bool):
        return "true" if value else "false", float(value), "boolean"
    if isinstance(value, (int, float)):
        return str(value), float(value), "number"
    if isinstance(value, (dict, list)):
        return _json(value), None, "json"
    return str(value), None, "string"


def _coerce_scalar(value: str) -> Any:
    stripped = value.strip()
    lowered = stripped.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None
    try:
        return int(stripped)
    except ValueError:
        try:
            return float(stripped)
        except ValueError:
            return stripped


def _json_mapping(value: Any) -> dict[str, Any]:
    if value in {None, ""}:
        return {}
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError, TypeError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _nonnegative_int(value: Any) -> int | None:
    try:
        parsed = int(str(value))
    except TypeError, ValueError:
        return None
    return parsed if parsed >= 0 else None


def _optional_string(value: Any) -> str | None:
    return None if value in {None, ""} else str(value)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_json(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except TypeError, json.JSONDecodeError:
        return default


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
