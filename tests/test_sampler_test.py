from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from pisa_sample_tools.sampler_test import collect_sampler_preview, default_sampler_for_source_type
from pisa_sample_tools.sampler_test_cli import main


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _write_param_range(path: Path) -> None:
    _write_yaml(
        path,
        {
            "parameters": [
                {"name": "speed", "type": "int", "values": [10, 20, 30]},
                {"name": "distance", "type": "double", "values": [1.5]},
            ]
        },
    )


def test_collect_sampler_preview_uses_grid_default_for_param_range(tmp_path: Path) -> None:
    source_file = tmp_path / "params.yaml"
    _write_param_range(source_file)

    result = collect_sampler_preview(source_file=source_file, max_samples=2)

    assert result.source_type == "param_range"
    assert result.sampler_name == "grid"
    assert result.total_samples == 3
    assert result.generated_samples == 2
    assert result.samples[0].index == 1
    assert result.samples[0].params == {"speed": 10, "distance": 1.5}
    assert result.samples[0].id is None


def test_collect_sampler_preview_preserves_explicit_ids_and_metadata(tmp_path: Path) -> None:
    source_file = tmp_path / "explicit.yaml"
    _write_yaml(
        source_file,
        {
            "samples": [
                {"id": "case-a", "params": {"speed": 10}, "metadata": {"tag": "a"}},
                {"id": 2, "params": {"speed": 20}},
            ]
        },
    )

    result = collect_sampler_preview(source_file=source_file, source_type="explicit")

    assert result.sampler_name == "explicit"
    assert [sample.id for sample in result.samples] == ["case-a", "2"]
    assert result.samples[0].metadata == {"source": "explicit", "index": 1}


def test_cli_sampler_test_outputs_table(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source_file = tmp_path / "params.yaml"
    _write_param_range(source_file)

    assert main([str(source_file), "--max-samples", "1"]) == 0

    captured = capsys.readouterr()
    assert "Source type: param_range" in captured.out
    assert "Sampler: grid" in captured.out
    assert "Generated samples: 1" in captured.out
    assert "speed" in captured.out
    assert "distance" in captured.out


def test_cli_sampler_test_outputs_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source_file = tmp_path / "params.yaml"
    _write_param_range(source_file)

    assert main([str(source_file), "--max-samples", "1", "--format", "json"]) == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["source_type"] == "param_range"
    assert data["sampler"] == "grid"
    assert data["generated_samples"] == 1
    assert data["samples"][0]["params"]["speed"] == 10


def test_default_sampler_for_source_type() -> None:
    assert default_sampler_for_source_type("openscenario") == "native"
    assert default_sampler_for_source_type("xosc") == "native"
    assert default_sampler_for_source_type("explicit") == "explicit"
    assert default_sampler_for_source_type("param_range") == "grid"


def test_cli_sampler_test_rejects_missing_source(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([str(tmp_path / "missing.yaml")])

    assert excinfo.value.code == 2
