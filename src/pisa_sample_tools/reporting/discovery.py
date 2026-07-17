from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

MANIFEST_NAMES = frozenset(
    {
        "execution_manifest.yaml",
        "execution_manifest.yml",
        "execution_manifest.json",
        "experiment_manifest.yaml",
        "experiment_manifest.yml",
        "experiment_manifest.json",
    }
)

TRACE_NAMES = (
    "frame_metrics.csv",
    "agent_states.csv",
    "agent_state.csv",
    "agent_geometry.csv",
    "collision_events.csv",
    "scenario_events.csv",
    "control_commands.csv",
)


@dataclass(frozen=True)
class ExperimentSource:
    dataset_id: str
    root: Path
    manifest_path: Path | None
    result_paths: tuple[Path, ...]
    missing_result_dirs: tuple[Path, ...]


def discover_experiments(source_roots: Path | Iterable[Path]) -> tuple[ExperimentSource, ...]:
    """Recursively discover experiments without opening any trace CSV files."""

    roots = _normalize_roots(source_roots)
    manifests: dict[Path, Path] = {}
    result_paths: list[Path] = []
    iteration_dirs: set[Path] = set()

    for source_root in roots:
        if not source_root.is_dir():
            raise ValueError(f"report source is not a directory: {source_root}")
        for current, dirnames, filenames in os.walk(source_root):
            current_path = Path(current)
            dirnames[:] = [
                name for name in dirnames if name not in {".git", "__pycache__", ".pytest_cache"}
            ]
            manifest_name = next((name for name in filenames if name in MANIFEST_NAMES), None)
            if manifest_name is not None:
                manifests[current_path] = current_path / manifest_name
            if current_path.name.startswith("iteration_"):
                iteration_dirs.add(current_path)
            if current_path.name == "monitor" and "result.csv" in filenames:
                result_paths.append(current_path / "result.csv")

    assignments: dict[Path, list[Path]] = {root: [] for root in manifests}
    unmanifested: dict[Path, list[Path]] = {}
    for result_path in result_paths:
        experiment_root = _nearest_manifest_root(result_path, manifests)
        if experiment_root is None:
            experiment_root = _fallback_experiment_root(result_path)
            unmanifested.setdefault(experiment_root, []).append(result_path)
        else:
            assignments.setdefault(experiment_root, []).append(result_path)

    # A manifest with no completed results is still useful for data-health reporting.
    roots_to_results = {**assignments, **unmanifested}
    sources: list[ExperimentSource] = []
    used_ids: set[str] = set()
    for experiment_root, paths in sorted(roots_to_results.items(), key=lambda item: str(item[0])):
        owning_root = max(
            (root for root in roots if experiment_root == root or root in experiment_root.parents),
            key=lambda item: len(item.parts),
        )
        dataset_id = _dataset_id(experiment_root, owning_root)
        if dataset_id in used_ids:
            suffix = hashlib.sha256(str(experiment_root).encode()).hexdigest()[:8]
            dataset_id = f"{dataset_id}--{suffix}"
        used_ids.add(dataset_id)
        missing = tuple(
            sorted(
                (
                    path
                    for path in iteration_dirs
                    if _belongs_to(path, experiment_root, manifests)
                    and not (path / "monitor" / "result.csv").is_file()
                ),
                key=_iteration_sort_key,
            )
        )
        sources.append(
            ExperimentSource(
                dataset_id=dataset_id,
                root=experiment_root,
                manifest_path=manifests.get(experiment_root),
                result_paths=tuple(sorted(paths, key=_result_sort_key)),
                missing_result_dirs=missing,
            )
        )
    return tuple(sources)


def discovery_fingerprint(
    source_roots: Path | Iterable[Path], sources: Iterable[ExperimentSource]
) -> str:
    """Cheap change detector over every file that affects the normalized index."""

    roots = _normalize_roots(source_roots)
    digest = hashlib.sha256()
    for root in roots:
        digest.update(f"root\0{root}\n".encode())
    for source in sorted(sources, key=lambda item: str(item.root)):
        digest.update(f"dataset\0{source.dataset_id}\0{source.root}\n".encode())
        if source.manifest_path is not None:
            _update_stat_digest(digest, source.manifest_path)
        for result_path in source.result_paths:
            _update_stat_digest(digest, result_path)
            monitor = result_path.parent
            for name in TRACE_NAMES:
                path = monitor / name
                if path.is_file():
                    _update_stat_digest(digest, path)
                else:
                    digest.update(f"missing\0{path}\n".encode())
        for missing in source.missing_result_dirs:
            digest.update(f"missing-result\0{missing}\n".encode())
    return digest.hexdigest()


def _normalize_roots(source_roots: Path | Iterable[Path]) -> tuple[Path, ...]:
    values = (source_roots,) if isinstance(source_roots, Path) else tuple(source_roots)
    if not values:
        raise ValueError("at least one report source root is required")
    return tuple(sorted({Path(value).expanduser().resolve() for value in values}, key=str))


def _nearest_manifest_root(path: Path, manifests: dict[Path, Path]) -> Path | None:
    return next((parent for parent in path.parents if parent in manifests), None)


def _fallback_experiment_root(result_path: Path) -> Path:
    iteration = next(
        (parent for parent in result_path.parents if parent.name.startswith("iteration_")), None
    )
    if iteration is not None:
        return iteration.parent
    # `<experiment>/monitor/result.csv`
    return result_path.parent.parent


def _belongs_to(path: Path, root: Path, manifests: dict[Path, Path]) -> bool:
    nearest = next((parent for parent in path.parents if parent in manifests), None)
    if nearest is not None:
        return nearest == root
    fallback = next(
        (parent.parent for parent in path.parents if parent.name.startswith("iteration_")), None
    )
    return fallback == root


def _dataset_id(experiment_root: Path, source_root: Path) -> str:
    relative = experiment_root.relative_to(source_root)
    return experiment_root.name if relative == Path(".") else relative.as_posix()


def _update_stat_digest(digest: object, path: Path) -> None:
    stat = path.stat()
    digest.update(f"file\0{path}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())


def _iteration_sort_key(path: Path) -> tuple[int, str]:
    suffix = path.name.removeprefix("iteration_")
    return (int(suffix), suffix) if suffix.isdigit() else (2**63 - 1, suffix)


def _result_sort_key(path: Path) -> tuple[int, str]:
    iteration = next(
        (parent for parent in path.parents if parent.name.startswith("iteration_")), None
    )
    return _iteration_sort_key(iteration) if iteration is not None else (0, str(path))
