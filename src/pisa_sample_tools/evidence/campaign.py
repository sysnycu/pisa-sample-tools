from __future__ import annotations

from pathlib import Path
from typing import Any

from pisa_sample_tools.common.yaml import load_mapping_file

from .models import DatasetSpec, EvidenceError


def load_campaign(path: Path) -> list[DatasetSpec]:
    raw = load_mapping_file(path, label="analysis campaign", error_type=EvidenceError)
    version = int(raw.get("version", 1))
    if version != 1:
        raise EvidenceError(f"unsupported analysis campaign version: {version}")
    datasets = raw.get("datasets")
    if not isinstance(datasets, list) or not datasets:
        raise EvidenceError("analysis campaign must contain a non-empty datasets list")
    base = path.expanduser().resolve().parent
    loaded: list[DatasetSpec] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(datasets, start=1):
        if not isinstance(item, dict):
            raise EvidenceError(f"campaign dataset #{index} must be a mapping")
        raw_path = item.get("results")
        if raw_path in {None, ""}:
            raise EvidenceError(f"campaign dataset #{index} must define results")
        results_path = Path(str(raw_path)).expanduser()
        if not results_path.is_absolute():
            results_path = (base / results_path).resolve()
        dataset_id = str(item.get("id") or results_path.name)
        if dataset_id in seen_ids:
            raise EvidenceError(f"duplicate campaign dataset id: {dataset_id}")
        seen_ids.add(dataset_id)
        metadata = _mapping(item.get("metadata"))
        xodr_path = metadata.get("xodr_path") or item.get("xodr_path")
        if xodr_path not in {None, ""}:
            resolved_xodr = Path(str(xodr_path)).expanduser()
            if not resolved_xodr.is_absolute():
                resolved_xodr = (base / resolved_xodr).resolve()
            metadata["xodr_path"] = str(resolved_xodr)
        labels = _mapping(item.get("labels"))
        grouping = _mapping(item.get("grouping"))
        metadata.update(
            {
                f"{key}_name": value
                for key, value in labels.items()
                if key in {"simulator", "av", "sampler"} and value not in {None, ""}
            }
        )
        metadata.update(
            {
                key: value
                for key, value in grouping.items()
                if key in {"repeat_id", "seed"} and value not in {None, ""}
            }
        )
        if item.get("logical_scenario_name") not in {None, ""}:
            metadata["logical_scenario_name"] = item["logical_scenario_name"]
        metadata["dataset_id"] = dataset_id
        loaded.append(
            DatasetSpec(
                dataset_id=dataset_id,
                results_path=results_path,
                metadata=metadata,
            )
        )
    return loaded


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}
