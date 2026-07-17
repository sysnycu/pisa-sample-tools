from __future__ import annotations

import csv
import hashlib
import hmac
import json
import secrets
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .errors import APIError
from .models import RepairPlan, RepairScanRequest
from .paths import PathPolicy

CSV_NAME = "agent_states.csv"


class RepairService:
    """Plan and apply the legacy initial-agent-state repair with explicit safety gates."""

    def __init__(self, policy: PathPolicy, secret: bytes | None = None) -> None:
        self.policy = policy
        self.secret = secret or secrets.token_bytes(32)

    def scan(self, request: RepairScanRequest) -> RepairPlan:
        source = self.policy.resolve(
            request.source_path, field="source_path", kind="directory"
        )
        init_state = (
            self.policy.resolve(request.init_state_path, field="init_state_path", kind="file")
            if request.init_state_path
            else None
        )
        reference = (
            self.policy.resolve(request.reference_root, field="reference_root", kind="directory")
            if request.reference_root
            else None
        )
        output = (
            self.policy.resolve(
                request.output_path,
                field="output_path",
                must_exist=False,
                kind="directory",
            )
            if request.output_path
            else None
        )
        if output is not None and (
            output == source
            or output.is_relative_to(source)
            or source.is_relative_to(output)
        ):
            raise APIError(
                400,
                "invalid_repair_output",
                "overlay output must not contain or equal the source directory",
                field="output_path",
            )
        if init_state is not None:
            _load_init_rows(init_state)
        files = sorted(source.glob(f"iteration_*/monitor/{CSV_NAME}"), key=_natural_key)
        if not files:
            raise APIError(
                404,
                "repair_sources_not_found",
                f"no {CSV_NAME} files were found",
                field="source_path",
            )
        changes: list[dict[str, Any]] = []
        findings: list[dict[str, Any]] = []
        for path in files:
            backup = Path(str(path) + request.backup_suffix)
            input_path = backup if backup.is_file() else path
            rows, fields = _read_csv(input_path)
            required = {"step_index", "sim_time_ms", "agent_id", "x", "y"}
            missing = sorted(required - set(fields))
            if missing:
                findings.append(
                    {
                        "severity": "error",
                        "code": "missing_columns",
                        "path": str(path.relative_to(source)),
                        "columns": missing,
                    }
                )
                continue
            if not rows:
                findings.append(
                    {
                        "severity": "warning",
                        "code": "empty_csv",
                        "path": str(path.relative_to(source)),
                    }
                )
                continue
            try:
                delta = request.time_step_ms or _infer_delta(rows)
                init_rows = (
                    _load_init_rows(init_state)
                    if init_state is not None
                    else _reference_rows(reference, source, path, request.backup_suffix)
                )
            except (OSError, ValueError) as exc:
                findings.append(
                    {
                        "severity": "error",
                        "code": "repair_input_invalid",
                        "path": str(path.relative_to(source)),
                        "message": str(exc),
                    }
                )
                continue
            changes.append(
                {
                    "path": str(path.relative_to(source)),
                    "sha256": _sha256(path),
                    "input_path": str(input_path.relative_to(source)),
                    "input_sha256": _sha256(input_path),
                    "original_rows": len(rows),
                    "inserted_rows": len(init_rows),
                    "result_rows": len(rows) + len(init_rows),
                    "time_shift_ms": delta,
                    "backup_exists": backup.is_file(),
                }
            )
        unsigned = {
            "version": 1,
            "source_path": str(source),
            "mode": request.mode,
            "output_path": str(output) if output else None,
            "init_state_path": str(init_state) if init_state else None,
            "reference_root": str(reference) if reference else None,
            "backup_suffix": request.backup_suffix,
            "time_step_ms": request.time_step_ms,
            "findings": findings,
            "changes": changes,
            "destructive": request.mode == "source",
        }
        return RepairPlan(signature=self._sign(unsigned), **unsigned)

    def apply(self, plan: RepairPlan, *, confirm_path: str | None, dry_run: bool) -> dict[str, Any]:
        unsigned = plan.model_dump(exclude={"signature"})
        if not hmac.compare_digest(plan.signature, self._sign(unsigned)):
            raise APIError(409, "repair_plan_invalid", "repair plan signature is invalid")
        if any(item.get("severity") == "error" for item in plan.findings):
            raise APIError(409, "repair_plan_blocked", "repair plan contains blocking findings")
        source = self.policy.resolve(plan.source_path, field="plan.source_path", kind="directory")
        output = (
            self.policy.resolve(
                plan.output_path,
                field="plan.output_path",
                must_exist=False,
                kind="directory",
            )
            if plan.output_path
            else None
        )
        if plan.mode == "source" and confirm_path != str(source):
            raise APIError(
                409,
                "repair_confirmation_required",
                "confirm_path must exactly match the resolved source path",
                field="confirm_path",
                details={"expected": str(source)},
            )
        init_path = Path(plan.init_state_path) if plan.init_state_path else None
        reference = Path(plan.reference_root) if plan.reference_root else None
        init_rows = _load_init_rows(init_path) if init_path else None
        patched: list[str] = []
        for change in plan.changes:
            path = (source / change["path"]).resolve()
            if not path.is_relative_to(source) or not path.is_file():
                raise APIError(409, "repair_source_changed", "a repair source file is missing")
            if _sha256(path) != change["sha256"]:
                raise APIError(
                    409,
                    "repair_source_changed",
                    "a repair source changed after the plan was created; scan again",
                    details={"path": str(path)},
                )
            input_path = (source / change.get("input_path", change["path"])).resolve()
            if (
                not input_path.is_relative_to(source)
                or not input_path.is_file()
                or _sha256(input_path) != change.get("input_sha256", change["sha256"])
            ):
                raise APIError(
                    409,
                    "repair_source_changed",
                    "the canonical repair input changed after the plan was created; scan again",
                    details={"path": str(input_path)},
                )
            rows, fields = _read_csv(input_path)
            selected_init = (
                init_rows
                if init_rows is not None
                else _reference_rows(reference, source, path, plan.backup_suffix)
            )
            delta = plan.time_step_ms or _infer_delta(rows)
            result = _patched_rows(rows, fields, selected_init, delta)
            target = path if plan.mode == "source" else output / path.relative_to(source)
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                if plan.mode == "source":
                    backup = Path(str(path) + plan.backup_suffix)
                    if not backup.exists():
                        shutil.copy2(path, backup)
                _write_csv_atomic(target, fields, result)
            patched.append(str(target))
        audit = {
            "generated_at": datetime.now(UTC).isoformat(),
            "operation": "agent_state_initialization",
            "mode": plan.mode,
            "source_path": str(source),
            "output_path": str(output) if output else None,
            "dry_run": dry_run,
            "files": patched,
            "backup_suffix": plan.backup_suffix if plan.mode == "source" else None,
        }
        if not dry_run:
            audit_root = source if plan.mode == "source" else output
            _append_audit(audit_root / ".pisa-repair-audit.jsonl", audit)
        return audit

    def restore(
        self,
        source_value: str,
        *,
        confirm_path: str,
        backup_suffix: str,
        dry_run: bool,
    ) -> dict[str, Any]:
        source = self.policy.resolve(source_value, field="source_path", kind="directory")
        if confirm_path != str(source):
            raise APIError(
                409,
                "repair_confirmation_required",
                "confirm_path must exactly match the resolved source path",
                field="confirm_path",
                details={"expected": str(source)},
            )
        backups = sorted(
            source.glob(f"iteration_*/monitor/{CSV_NAME}{backup_suffix}"), key=_natural_key
        )
        if not backups:
            raise APIError(404, "repair_backups_not_found", "no repair backups were found")
        targets = []
        for backup in backups:
            target = backup.with_name(CSV_NAME)
            if not dry_run:
                _copy_atomic(backup, target)
            targets.append(str(target))
        audit = {
            "generated_at": datetime.now(UTC).isoformat(),
            "operation": "restore_agent_state_backups",
            "source_path": str(source),
            "dry_run": dry_run,
            "files": targets,
            "backup_suffix": backup_suffix,
        }
        if not dry_run:
            _append_audit(source / ".pisa-repair-audit.jsonl", audit)
        return audit

    def _sign(self, value: dict[str, Any]) -> str:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return hmac.new(self.secret, payload, hashlib.sha256).hexdigest()


def _load_init_rows(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        raise ValueError("initial state path is missing")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    agents = raw.get("agents") if isinstance(raw, dict) else raw
    if isinstance(agents, dict):
        result = []
        for agent_id, values in agents.items():
            if not isinstance(values, dict):
                raise ValueError(f"agent {agent_id!r} must be a mapping")
            result.append({**values, "agent_id": agent_id})
        return result
    if isinstance(agents, list) and all(
        isinstance(value, dict) and "agent_id" in value for value in agents
    ):
        return [dict(value) for value in agents]
    raise ValueError("initial state YAML must contain an agents mapping or list")


def _reference_rows(
    reference_root: Path | None,
    target_root: Path,
    target_path: Path,
    backup_suffix: str,
) -> list[dict[str, Any]]:
    if reference_root is None:
        raise ValueError("reference root is missing")
    candidate = reference_root / target_path.relative_to(target_root)
    backup = Path(str(candidate) + backup_suffix)
    source = backup if backup.is_file() else candidate
    if not source.is_file():
        raise ValueError(f"reference CSV not found: {candidate}")
    rows, _ = _read_csv(source)
    steps = [int(float(row["step_index"])) for row in rows if row.get("step_index")]
    if not steps:
        raise ValueError(f"reference CSV has no step rows: {source}")
    first = min(steps)
    return [row for row in rows if int(float(row["step_index"])) == first]


def _read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, skipinitialspace=True)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        fields = [field.strip() for field in reader.fieldnames]
        rows = [
            {(key or "").strip(): (value or "").strip() for key, value in row.items()}
            for row in reader
        ]
    return rows, fields


def _infer_delta(rows: list[dict[str, str]]) -> float:
    times = sorted({float(row["sim_time_ms"]) for row in rows if row.get("sim_time_ms")})
    for previous, current in zip(times, times[1:], strict=False):
        if current > previous:
            return current - previous
    raise ValueError("could not infer a positive sim_time_ms delta")


def _patched_rows(
    rows: list[dict[str, str]],
    fields: list[str],
    init_rows: list[dict[str, Any]],
    delta: float,
) -> list[dict[str, str]]:
    initial = []
    for values in init_rows:
        row = {field: "" for field in fields}
        for field in fields:
            key = field.strip()
            if key == "step_index":
                row[field] = "0"
            elif key == "sim_time_ms":
                row[field] = "0.000000"
            elif key in values:
                row[field] = str(values[key])
        initial.append(row)
    shifted = []
    for source in rows:
        row = dict(source)
        row["step_index"] = str(int(float(row["step_index"])) + 1)
        row["sim_time_ms"] = f"{float(row['sim_time_ms']) + delta:.6f}"
        shifted.append(row)
    return [*initial, *shifted]


def _write_csv_atomic(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    with tempfile.NamedTemporaryFile(
        "w", newline="", encoding="utf-8", dir=path.parent, delete=False, prefix=f".{path.name}."
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        temporary = Path(handle.name)
    temporary.replace(path)


def _copy_atomic(source: Path, target: Path) -> None:
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False, prefix=f".{target.name}.") as handle:
        temporary = Path(handle.name)
    shutil.copy2(source, temporary)
    temporary.replace(target)


def _append_audit(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _natural_key(path: Path) -> tuple[Any, ...]:
    result: list[Any] = []
    for part in path.parts:
        suffix = part.removeprefix("iteration_")
        result.append((0, int(suffix)) if suffix.isdigit() else (1, part))
    return tuple(result)
