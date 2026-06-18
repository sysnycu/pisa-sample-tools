from __future__ import annotations

import argparse
import csv
import shutil
from pathlib import Path
from typing import Any

import yaml

CSV_NAME = "agent_states.csv"


def main() -> int:
    parser = argparse.ArgumentParser(description="Patch CARLA agent_states.csv files with explicit step 0 rows.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    apply_parser = subparsers.add_parser("apply", help="Insert init rows into agent_states.csv files.")
    apply_parser.add_argument("root", type=Path, help="Result root containing iteration_*/monitor/agent_states.csv.")
    apply_parser.add_argument(
        "init_state",
        nargs="?",
        type=Path,
        help="YAML file describing initial state per agent. Omit when using --reference-root.",
    )
    apply_parser.add_argument(
        "--reference-root",
        type=Path,
        help="Reference result root containing matching iteration_*/monitor/agent_states.csv files.",
    )
    apply_parser.add_argument("--backup-suffix", default=".bak", help="Backup suffix for original CSV files.")
    apply_parser.add_argument(
        "--time-step-ms",
        type=float,
        help="Time delta added to existing rows. Defaults to the first positive sim_time_ms delta in each file.",
    )
    apply_parser.add_argument("--dry-run", action="store_true", help="Show files that would be patched.")

    restore_parser = subparsers.add_parser("restore", help="Restore agent_states.csv from its backup.")
    restore_parser.add_argument("root", type=Path, help="Result root containing iteration_*/monitor backups.")
    restore_parser.add_argument("--backup-suffix", default=".bak", help="Backup suffix to restore from.")
    restore_parser.add_argument("--dry-run", action="store_true", help="Show files that would be restored.")

    args = parser.parse_args()
    if args.command == "apply":
        if bool(args.init_state) == bool(args.reference_root):
            parser.error("provide either init_state or --reference-root")
        init_rows = load_init_rows(args.init_state) if args.init_state is not None else None
        return apply_init_state(
            args.root,
            init_rows=init_rows,
            reference_root=args.reference_root,
            backup_suffix=args.backup_suffix,
            time_step_ms=args.time_step_ms,
            dry_run=args.dry_run,
        )
    if args.command == "restore":
        return restore_backups(args.root, backup_suffix=args.backup_suffix, dry_run=args.dry_run)
    raise AssertionError(args.command)


def apply_init_state(
    root: Path,
    *,
    init_rows: list[dict[str, Any]] | None,
    reference_root: Path | None,
    backup_suffix: str,
    time_step_ms: float | None,
    dry_run: bool,
) -> int:
    if (init_rows is None) == (reference_root is None):
        raise ValueError("provide either init_rows or reference_root")
    files = sorted(root.glob(f"iteration_*/monitor/{CSV_NAME}"), key=natural_path_key)
    if not files:
        print(f"No {CSV_NAME} files found under {root}")
        return 1

    patched = 0
    for path in files:
        backup_path = Path(str(path) + backup_suffix)
        source_path = backup_path if backup_path.exists() else path
        rows, fieldnames = read_csv(source_path)
        if not rows:
            print(f"[skip] empty CSV: {source_path}")
            continue
        current_init_rows = (
            init_rows
            if init_rows is not None
            else load_reference_init_rows(reference_root, root, path, backup_suffix=backup_suffix)
        )
        delta_ms = time_step_ms if time_step_ms is not None else infer_time_step_ms(rows)
        patched_rows = build_patched_rows(rows, fieldnames, current_init_rows, delta_ms=delta_ms)
        if backup_path.exists():
            print(f"[patch] {path} from {backup_path.name} dt={delta_ms:g}ms")
        else:
            print(f"[patch] {path} dt={delta_ms:g}ms backup={backup_path.name}")
        if not dry_run:
            if not backup_path.exists():
                shutil.copy2(path, backup_path)
            write_csv(path, fieldnames, patched_rows)
        patched += 1
    print(f"patched: {patched}")
    return 0


def build_patched_rows(
    rows: list[dict[str, str]],
    fieldnames: list[str],
    init_rows: list[dict[str, Any]],
    *,
    delta_ms: float,
) -> list[dict[str, str]]:
    init_output = [format_init_row(init_row, fieldnames) for init_row in init_rows]
    shifted = []
    for row in rows:
        copy = dict(row)
        copy["step_index"] = format_int(parse_int(copy["step_index"]) + 1)
        copy["sim_time_ms"] = format_float(parse_float(copy["sim_time_ms"]) + delta_ms)
        shifted.append(copy)
    return [*init_output, *shifted]


def load_reference_init_rows(
    reference_root: Path,
    target_root: Path,
    target_path: Path,
    *,
    backup_suffix: str,
) -> list[dict[str, Any]]:
    reference_path = resolve_reference_path(reference_root, target_root, target_path)
    reference_source = select_source_csv(reference_path, backup_suffix=backup_suffix)
    rows, _fieldnames = read_csv(reference_source)
    if not rows:
        raise ValueError(f"reference CSV has no rows: {reference_source}")
    first_step = min(parse_int(row["step_index"]) for row in rows if row.get("step_index", "").strip())
    return [dict(row) for row in rows if parse_int(row["step_index"]) == first_step]


def resolve_reference_path(reference_root: Path, target_root: Path, target_path: Path) -> Path:
    try:
        relative = target_path.relative_to(target_root)
    except ValueError as exc:
        raise ValueError(f"target path is not under target root: {target_path}") from exc
    candidate = reference_root / relative
    if candidate.exists():
        return candidate
    raise FileNotFoundError(f"reference CSV not found for {target_path}: {candidate}")


def restore_backups(root: Path, *, backup_suffix: str, dry_run: bool) -> int:
    backup_name = f"{CSV_NAME}{backup_suffix}"
    files = sorted(root.glob(f"iteration_*/monitor/{backup_name}"), key=natural_path_key)
    if not files:
        print(f"No {backup_name} files found under {root}")
        return 1

    restored = 0
    for backup_path in files:
        target_path = backup_path.with_name(CSV_NAME)
        print(f"[restore] {backup_path} -> {target_path}")
        if not dry_run:
            shutil.copy2(backup_path, target_path)
        restored += 1
    print(f"restored: {restored}")
    return 0


def format_init_row(values: dict[str, Any], fieldnames: list[str]) -> dict[str, str]:
    row = {field: "" for field in fieldnames}
    for field in fieldnames:
        key = field.strip()
        if key == "step_index":
            row[field] = format_int(0)
        elif key == "sim_time_ms":
            row[field] = format_float(0.0)
        elif key in values:
            value = values[key]
            if key == "agent_id":
                row[field] = format_int(int(value))
            elif isinstance(value, int):
                row[field] = format_int(value)
            elif isinstance(value, float):
                row[field] = format_float(value)
            else:
                row[field] = str(value)
    return row


def load_init_rows(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    agents = raw.get("agents") if isinstance(raw, dict) else raw
    if isinstance(agents, dict):
        rows = []
        for agent_id, values in agents.items():
            if not isinstance(values, dict):
                raise ValueError(f"agent {agent_id!r} must be a mapping")
            row = dict(values)
            row["agent_id"] = agent_id
            rows.append(row)
        return rows
    if isinstance(agents, list):
        rows = []
        for values in agents:
            if not isinstance(values, dict) or "agent_id" not in values:
                raise ValueError("list-style agents must be mappings with agent_id")
            rows.append(dict(values))
        return rows
    raise ValueError("init_state YAML must contain an agents mapping or list")


def infer_time_step_ms(rows: list[dict[str, str]]) -> float:
    times = sorted({parse_float(row["sim_time_ms"]) for row in rows if row.get("sim_time_ms", "").strip()})
    for previous, current in zip(times, times[1:], strict=False):
        delta = current - previous
        if delta > 0:
            return delta
    raise ValueError("could not infer positive sim_time_ms delta; pass --time-step-ms")


def read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, skipinitialspace=True)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        return [normalize_row(row) for row in reader], [field.strip() for field in reader.fieldnames]


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def normalize_row(row: dict[str | None, str | None]) -> dict[str, str]:
    return {(key or "").strip(): (value or "").strip() for key, value in row.items()}


def select_source_csv(path: Path, *, backup_suffix: str = ".bak") -> Path:
    backup_path = Path(str(path) + backup_suffix)
    if backup_path.exists():
        return backup_path
    return path


def natural_path_key(path: Path) -> tuple[Any, ...]:
    parts: list[Any] = []
    for part in path.parts:
        if part.startswith("iteration_"):
            suffix = part.removeprefix("iteration_")
            if suffix.isdigit():
                parts.append(("iteration", int(suffix)))
                continue
        parts.append(part)
    return tuple(parts)


def parse_int(value: str) -> int:
    return int(float(value.strip()))


def parse_float(value: str) -> float:
    return float(value.strip())


def format_int(value: int) -> str:
    return str(value)


def format_float(value: float) -> str:
    return f"{value:.6f}"


if __name__ == "__main__":
    raise SystemExit(main())
