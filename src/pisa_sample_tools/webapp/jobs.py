from __future__ import annotations

import json
import queue
import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from fastapi.encoders import jsonable_encoder

from .errors import JobCancelled
from .models import ErrorBody, Job, JobEvent, Progress

TERMINAL_STATES = {"succeeded", "failed", "cancelled"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, Enum):
        return value.value
    return jsonable_encoder(value)


class JobContext:
    def __init__(self, manager: JobManager, job_id: str) -> None:
        self.manager = manager
        self.job_id = job_id

    def check_cancelled(self) -> None:
        if self.manager.cancel_requested(self.job_id):
            raise JobCancelled("job cancellation requested")

    def progress(
        self,
        phase: str,
        *,
        current: float | None = None,
        total: float | None = None,
        unit: str | None = None,
        message: str | None = None,
    ) -> None:
        self.check_cancelled()
        self.manager.update_progress(
            self.job_id,
            phase=phase,
            current=current,
            total=total,
            unit=unit,
            message=message,
        )

    def log(self, message: str, *, stream: str = "system") -> None:
        self.manager.add_event(self.job_id, "log", {"message": message, "stream": stream})

    def artifact(self, path: str | Path, *, kind: str | None = None) -> None:
        self.manager.add_event(
            self.job_id,
            "artifact",
            {"path": str(path), **({"kind": kind} if kind else {})},
        )


class JobManager:
    """Small SQLite-backed job/event store with cooperative cancellation."""

    def __init__(self, database: Path | str | None = None, *, max_workers: int = 2) -> None:
        if not isinstance(max_workers, int) or isinstance(max_workers, bool) or not 1 <= max_workers <= 16:
            raise ValueError("max_workers must be an integer between 1 and 16")
        if database is None:
            database = ":memory:"
        if str(database) != ":memory:":
            path = Path(database).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            database = str(path)
        self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._tasks: queue.Queue[tuple[str, Callable[[JobContext], Any]]] = queue.Queue()
        self._workers: list[threading.Thread] = []
        self._init_schema()
        for index in range(max_workers):
            worker = threading.Thread(
                target=self._worker,
                daemon=True,
                name=f"pisa-job-worker-{index + 1}",
            )
            worker.start()
            self._workers.append(worker)

    def _init_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    progress_current REAL,
                    progress_total REAL,
                    progress_unit TEXT,
                    message TEXT,
                    request_json TEXT NOT NULL,
                    result_json TEXT,
                    error_json TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS job_events (
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (job_id, sequence)
                );
                """
            )
            columns = {row[1] for row in self._connection.execute("PRAGMA table_info(jobs)")}
            if "message" not in columns:
                self._connection.execute("ALTER TABLE jobs ADD COLUMN message TEXT")
            interrupted = self._connection.execute(
                "SELECT id FROM jobs WHERE status IN ('queued', 'running')"
            ).fetchall()
            for row in interrupted:
                error = ErrorBody(
                    code="job_interrupted",
                    message="the application stopped before this job completed",
                    request_id=row["id"],
                )
                self._connection.execute(
                    "UPDATE jobs SET status='failed', phase='interrupted', error_json=?, "
                    "completed_at=? WHERE id=?",
                    (error.model_dump_json(), _now(), row["id"]),
                )

    def submit(
        self,
        kind: str,
        request: dict[str, Any],
        task: Callable[[JobContext], Any],
    ) -> Job:
        job_id = uuid.uuid4().hex
        created_at = _now()
        request_json = json.dumps(_jsonable(request), sort_keys=True)
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO jobs "
                "(id, kind, status, phase, request_json, created_at) "
                "VALUES (?, ?, 'queued', 'queued', ?, ?)",
                (job_id, kind, request_json, created_at),
            )
            self._insert_event(job_id, "queued", {"kind": kind}, created_at=created_at)
        self._tasks.put((job_id, task))
        return self.get(job_id)

    def _worker(self) -> None:
        while True:
            job_id, task = self._tasks.get()
            try:
                self._execute(job_id, task)
            finally:
                self._tasks.task_done()

    def _execute(self, job_id: str, task: Callable[[JobContext], Any]) -> None:
        context = JobContext(self, job_id)
        try:
            context.check_cancelled()
            with self._lock, self._connection:
                started_at = _now()
                self._connection.execute(
                    "UPDATE jobs SET status='running', phase='starting', started_at=? WHERE id=?",
                    (started_at, job_id),
                )
                self._insert_event(job_id, "progress", {"phase": "starting"})
            result = task(context)
            context.check_cancelled()
            payload = json.dumps(_jsonable(result), sort_keys=True)
            with self._lock, self._connection:
                completed_at = _now()
                self._connection.execute(
                    "UPDATE jobs SET status='succeeded', phase='complete', result_json=?, "
                    "completed_at=? WHERE id=?",
                    (payload, completed_at, job_id),
                )
                self._insert_event(job_id, "complete", {"result": _jsonable(result)})
        except JobCancelled:
            with self._lock, self._connection:
                completed_at = _now()
                self._connection.execute(
                    "UPDATE jobs SET status='cancelled', phase='cancelled', completed_at=? "
                    "WHERE id=?",
                    (completed_at, job_id),
                )
                self._insert_event(job_id, "cancelled", {"message": "job cancelled"})
        except Exception as exc:  # the exception is persisted for polling clients
            error = ErrorBody(
                code=f"{self.get(job_id).kind}_failed",
                message=str(exc) or type(exc).__name__,
                details={"exception": type(exc).__name__},
                request_id=job_id,
            )
            with self._lock, self._connection:
                completed_at = _now()
                self._connection.execute(
                    "UPDATE jobs SET status='failed', phase='failed', error_json=?, "
                    "completed_at=? WHERE id=?",
                    (error.model_dump_json(), completed_at, job_id),
                )
                self._insert_event(job_id, "failed", {"error": error.model_dump()})
        finally:
            with self._changed:
                self._changed.notify_all()

    def list(self, *, limit: int = 100, status: str | None = None) -> list[Job]:
        limit = max(1, min(limit, 1_000))
        with self._lock:
            if status is None:
                rows = self._connection.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM jobs WHERE status=? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def get(self, job_id: str) -> Job:
        with self._lock:
            row = self._connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._row_to_job(row)

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.status in TERMINAL_STATES:
            return job
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE jobs SET cancel_requested=1, phase='cancelling' WHERE id=?", (job_id,)
            )
            self._insert_event(job_id, "progress", {"phase": "cancelling"})
        return self.get(job_id)

    def cancel_requested(self, job_id: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def update_progress(
        self,
        job_id: str,
        *,
        phase: str,
        current: float | None = None,
        total: float | None = None,
        unit: str | None = None,
        message: str | None = None,
    ) -> None:
        data = {
            "phase": phase,
            "progress": {"current": current, "total": total, "unit": unit},
        }
        if message:
            data["message"] = message
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE jobs SET phase=?, progress_current=?, progress_total=?, progress_unit=?, message=? "
                "WHERE id=?",
                (phase, current, total, unit, message, job_id),
            )
            self._insert_event(job_id, "progress", data)

    def add_event(self, job_id: str, event_type: str, data: dict[str, Any]) -> JobEvent:
        self.get(job_id)
        with self._lock, self._connection:
            return self._insert_event(job_id, event_type, _jsonable(data))

    def events(self, job_id: str, *, after: int = 0, limit: int = 1_000) -> list[JobEvent]:
        self.get(job_id)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM job_events WHERE job_id=? AND sequence>? "
                "ORDER BY sequence LIMIT ?",
                (job_id, after, max(1, min(limit, 10_000))),
            ).fetchall()
        return [
            JobEvent(
                sequence=row["sequence"],
                type=row["type"],
                data=json.loads(row["data_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def wait_for_change(self, timeout: float = 10.0) -> None:
        with self._changed:
            self._changed.wait(timeout=timeout)

    def _insert_event(
        self,
        job_id: str,
        event_type: str,
        data: dict[str, Any],
        *,
        created_at: str | None = None,
    ) -> JobEvent:
        sequence = self._connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM job_events WHERE job_id=?", (job_id,)
        ).fetchone()[0]
        created_at = created_at or _now()
        self._connection.execute(
            "INSERT INTO job_events (job_id, sequence, type, data_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (job_id, sequence, event_type, json.dumps(_jsonable(data), sort_keys=True), created_at),
        )
        self._changed.notify_all()
        return JobEvent(sequence=sequence, type=event_type, data=data, created_at=created_at)

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            kind=row["kind"],
            status=row["status"],
            phase=row["phase"],
            message=row["message"],
            progress=Progress(
                current=row["progress_current"],
                total=row["progress_total"],
                unit=row["progress_unit"],
            ),
            request=json.loads(row["request_json"]),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=ErrorBody.model_validate_json(row["error_json"]) if row["error_json"] else None,
            cancel_requested=bool(row["cancel_requested"]),
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )
