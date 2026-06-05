from __future__ import annotations

from pathlib import Path

from pisa_sample_tools.common.sorting import natural_key
from pisa_sample_tools.trajectory import (
    AGENT_STATE_FILENAMES,
    discover_agent_state_files,
)

from .models import TrajectoryCompareError


def pair_agent_state_files(left_path: Path, right_path: Path) -> list[tuple[str, Path, Path]]:
    if left_path.is_file() and right_path.is_file():
        return [("comparison", _validate_agent_state_file(left_path), _validate_agent_state_file(right_path))]
    if _is_single_iteration_path(left_path) and _is_single_iteration_path(right_path):
        return [
            (
                _pair_name(left_path, right_path),
                _single_agent_state_file(left_path),
                _single_agent_state_file(right_path),
            )
        ]

    left_files = _agent_state_file_map(left_path)
    right_files = _agent_state_file_map(right_path)
    names = sorted(set(left_files) & set(right_files), key=natural_key)
    return [(name, left_files[name], right_files[name]) for name in names]


def _agent_state_file_map(path: Path) -> dict[str, Path]:
    if path.is_file():
        return {"comparison": _validate_agent_state_file(path)}
    if _is_single_iteration_path(path):
        return {_iteration_name(path): _single_agent_state_file(path)}
    files: dict[str, Path] = {}
    for source_file in discover_agent_state_files(path):
        name = _iteration_name_from_file(source_file)
        files[name] = source_file
    return files


def _validate_agent_state_file(path: Path) -> Path:
    if not path.is_file() or path.name not in AGENT_STATE_FILENAMES:
        raise TrajectoryCompareError(f"expected agent state csv file: {path}")
    return path


def _single_agent_state_file(path: Path) -> Path:
    files = discover_agent_state_files(path)
    if len(files) != 1:
        raise TrajectoryCompareError(f"expected one agent state file below {path}, found {len(files)}")
    return files[0]


def _is_single_iteration_path(path: Path) -> bool:
    return path.is_dir() and path.name.startswith("iteration_")


def _iteration_name(path: Path) -> str:
    return path.name if path.name.startswith("iteration_") else path.stem


def _iteration_name_from_file(path: Path) -> str:
    for parent in path.parents:
        if parent.name.startswith("iteration_"):
            return parent.name
    return path.stem


def _pair_name(left_path: Path, right_path: Path) -> str:
    left = _iteration_name(left_path)
    right = _iteration_name(right_path)
    return left if left == right else f"{left}_vs_{right}"

