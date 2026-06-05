from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from pisa_sample_tools.cli import main
from pisa_sample_tools.exporter import export_samples


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _make_runner_fixture(
    tmp_path: Path,
    *,
    sample_count: int = 5,
    outputs: dict[str, Any] | None = None,
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    scenario_dir = tmp_path / "scenario"
    config_dir = tmp_path / "configs"
    scenario_dir.mkdir()
    config_dir.mkdir()
    (scenario_dir / "demo_scenario.xosc").write_text("<OpenSCENARIO />\n", encoding="utf-8")
    _write_yaml(
        scenario_dir / "spec.yaml",
        {"scenario_name": "demo_scenario", "map_name": "test_map"},
    )
    _write_yaml(
        scenario_dir / "stop_conditions.yaml",
        [{"type": "timeout", "name": "timeout", "timeout_ms": 1000}],
    )
    params_config: dict[str, Any] = {
        "parameters": [
            {
                "name": "speed",
                "type": "int",
                "values": list(range(1, sample_count + 1)),
            }
        ]
    }
    if outputs is not None:
        params_config["outputs"] = outputs
    _write_yaml(scenario_dir / "params.yaml", params_config)
    sampler_config = config_dir / "grid.yaml"
    _write_yaml(
        sampler_config,
        {
            "source": {"type": "param_range", "path": "params.yaml"},
            "defaults": {"n": 1},
        },
    )
    runner_spec = tmp_path / "runner.json"
    runner_spec.write_text(
        json.dumps(
            {
                "scenario": {
                    "scenario_path": str(scenario_dir),
                    "stop_condition_config_path": str(scenario_dir / "stop_conditions.yaml"),
                },
                "sampler": {"name": "grid", "config_path": str(sampler_config)},
            }
        ),
        encoding="utf-8",
    )
    return runner_spec


def test_source_path_uses_scenario_path_as_base(tmp_path: Path) -> None:
    runner_spec = _make_runner_fixture(tmp_path, sample_count=2)
    output_dir = tmp_path / "out"

    export_samples(runner_spec_path=runner_spec, output_dir=output_dir, shard_size=10)

    manifest = yaml.safe_load((output_dir / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["source_path"] == str(tmp_path / "scenario" / "params.yaml")
    assert manifest["source_type"] == "param_range"


def test_shard_size_splits_samples(tmp_path: Path) -> None:
    runner_spec = _make_runner_fixture(tmp_path, sample_count=5)
    output_dir = tmp_path / "out"

    export_samples(runner_spec_path=runner_spec, output_dir=output_dir, shard_size=2)

    manifest = yaml.safe_load((output_dir / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["shard_count"] == 3
    assert [shard["sample_count"] for shard in manifest["shards"]] == [2, 2, 1]
    assert (output_dir / "demo_scenario-grid1" / "explicit_samples.yaml").exists()
    assert (output_dir / "demo_scenario-grid1" / "demo_scenario.xosc").exists()
    assert (output_dir / "demo_scenario-grid1" / "spec.yaml").exists()
    assert (output_dir / "demo_scenario-grid1" / "stop_conditions.yaml").exists()


def test_num_shards_splits_samples(tmp_path: Path) -> None:
    runner_spec = _make_runner_fixture(tmp_path, sample_count=5)
    output_dir = tmp_path / "out"

    export_samples(runner_spec_path=runner_spec, output_dir=output_dir, num_shards=2)

    manifest = yaml.safe_load((output_dir / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["shard_count"] == 2
    assert [shard["sample_count"] for shard in manifest["shards"]] == [3, 2]


def test_missing_sample_id_gets_stable_id(tmp_path: Path) -> None:
    runner_spec = _make_runner_fixture(tmp_path, sample_count=2)
    output_dir = tmp_path / "out"

    export_samples(runner_spec_path=runner_spec, output_dir=output_dir, shard_size=10)

    shard = yaml.safe_load(
        (output_dir / "demo_scenario-grid1" / "explicit_samples.yaml").read_text(encoding="utf-8")
    )
    assert [sample["id"] for sample in shard["samples"]] == ["1", "2"]


def test_export_writes_only_sim_params_without_metadata(tmp_path: Path) -> None:
    runner_spec = _make_runner_fixture(
        tmp_path,
        sample_count=1,
        outputs={"ego_speed": {"expression": "speed * 2", "type": "int"}},
    )
    output_dir = tmp_path / "out"

    export_samples(runner_spec_path=runner_spec, output_dir=output_dir, shard_size=10)

    shard = yaml.safe_load(
        (output_dir / "demo_scenario-grid1" / "explicit_samples.yaml").read_text(encoding="utf-8")
    )
    assert shard["samples"] == [{"id": "1", "params": {"ego_speed": 2}}]


def test_manifest_contains_shard_details(tmp_path: Path) -> None:
    runner_spec = _make_runner_fixture(tmp_path, sample_count=3)
    output_dir = tmp_path / "out"

    export_samples(runner_spec_path=runner_spec, output_dir=output_dir, shard_size=2)

    manifest = yaml.safe_load((output_dir / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["runner_spec_path"] == str(runner_spec)
    assert manifest["scenario_name"] == "demo_scenario"
    assert manifest["sampler_name"] == "grid"
    assert manifest["sampler_config_path"] == str(tmp_path / "configs" / "grid.yaml")
    assert manifest["total_samples"] == 3
    assert manifest["shard_size"] == 2
    assert manifest["num_shards"] is None
    assert manifest["shards"][0]["index"] == 0
    assert manifest["shards"][0]["bundle_id"] == 1
    assert manifest["shards"][0]["sample_count"] == 2
    assert manifest["shards"][0]["bundle_path"] == str(output_dir / "demo_scenario-grid1")
    assert manifest["shards"][0]["sample_file_path"] == str(
        output_dir / "demo_scenario-grid1" / "explicit_samples.yaml"
    )
    assert manifest["shards"][0]["scenario_file_path"] == str(
        output_dir / "demo_scenario-grid1" / "demo_scenario.xosc"
    )
    assert manifest["shards"][0]["first_sample_id"] == "1"
    assert manifest["shards"][0]["last_sample_id"] == "2"


def test_output_dir_exists_without_overwrite_errors(tmp_path: Path) -> None:
    runner_spec = _make_runner_fixture(tmp_path, sample_count=1)
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    with pytest.raises(ValueError, match="already exists"):
        export_samples(runner_spec_path=runner_spec, output_dir=output_dir, shard_size=1)


def test_shard_size_and_num_shards_together_error(tmp_path: Path) -> None:
    runner_spec = _make_runner_fixture(tmp_path, sample_count=1)

    with pytest.raises(ValueError, match="mutually exclusive"):
        export_samples(
            runner_spec_path=runner_spec,
            output_dir=tmp_path / "out",
            shard_size=1,
            num_shards=1,
        )


def test_cli_rejects_shard_size_and_num_shards_together(tmp_path: Path) -> None:
    runner_spec = _make_runner_fixture(tmp_path, sample_count=1)

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "--runner-spec",
                str(runner_spec),
                "--output-dir",
                str(tmp_path / "out"),
                "--shard-size",
                "1",
                "--num-shards",
                "1",
            ]
        )

    assert excinfo.value.code == 2


def test_sampler_spec_input_builds_bundle(tmp_path: Path) -> None:
    runner_spec = _make_runner_fixture(tmp_path, sample_count=2)
    runner_data = json.loads(runner_spec.read_text(encoding="utf-8"))
    sampler_spec = tmp_path / "sampler.yaml"
    _write_yaml(sampler_spec, runner_data["sampler"])
    output_dir = tmp_path / "out"

    export_samples(
        sampler_spec_path=sampler_spec,
        scenario_path=tmp_path / "scenario",
        output_dir=output_dir,
        shard_size=2,
    )

    assert (output_dir / "demo_scenario-grid1" / "explicit_samples.yaml").exists()


def test_missing_required_scenario_file_errors(tmp_path: Path) -> None:
    runner_spec = _make_runner_fixture(tmp_path, sample_count=1)
    (tmp_path / "scenario" / "spec.yaml").unlink()

    with pytest.raises(ValueError, match="spec.yaml"):
        export_samples(runner_spec_path=runner_spec, output_dir=tmp_path / "out", shard_size=1)


def test_zip_output_excludes_manifest(tmp_path: Path) -> None:
    runner_spec = _make_runner_fixture(tmp_path, sample_count=2)
    output_dir = tmp_path / "out"

    result = export_samples(
        runner_spec_path=runner_spec,
        output_dir=output_dir,
        shard_size=1,
        create_zip=True,
    )

    assert result.zip_path == tmp_path / "out.zip"
    assert result.zip_path.exists()
    with zipfile.ZipFile(result.zip_path) as archive:
        names = archive.namelist()

    assert "manifest.yaml" not in names
    assert all(not name.endswith("/manifest.yaml") for name in names)
    assert "demo_scenario-grid1/explicit_samples.yaml" in names
    assert "demo_scenario-grid2/demo_scenario.xosc" in names


def test_dry_run_computes_summary_without_writing(tmp_path: Path) -> None:
    runner_spec = _make_runner_fixture(tmp_path, sample_count=3)
    output_dir = tmp_path / "out"

    result = export_samples(
        runner_spec_path=runner_spec,
        output_dir=output_dir,
        shard_size=2,
        create_zip=True,
        dry_run=True,
    )

    assert result.dry_run is True
    assert result.total_samples == 3
    assert result.shard_count == 2
    assert result.summary is not None
    assert result.summary["output_dir"] == str(output_dir)
    assert result.summary["zip_path"] == str(tmp_path / "out.zip")
    assert not output_dir.exists()
    assert not (tmp_path / "out.zip").exists()


def test_overwrite_replaces_previous_tool_output(tmp_path: Path) -> None:
    first_runner_spec = _make_runner_fixture(tmp_path / "first", sample_count=4)
    second_runner_spec = _make_runner_fixture(tmp_path / "second", sample_count=2)
    output_dir = tmp_path / "out"

    export_samples(runner_spec_path=first_runner_spec, output_dir=output_dir, shard_size=1)
    assert (output_dir / "demo_scenario-grid4").exists()

    export_samples(
        runner_spec_path=second_runner_spec,
        output_dir=output_dir,
        shard_size=1,
        overwrite=True,
    )

    assert (output_dir / "demo_scenario-grid1").exists()
    assert (output_dir / "demo_scenario-grid2").exists()
    assert not (output_dir / "demo_scenario-grid3").exists()
    assert not (output_dir / "demo_scenario-grid4").exists()


def test_overwrite_rejects_non_tool_output_dir(tmp_path: Path) -> None:
    runner_spec = _make_runner_fixture(tmp_path, sample_count=1)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    (output_dir / "unrelated.txt").write_text("keep me\n", encoding="utf-8")

    with pytest.raises(ValueError, match="no manifest.yaml"):
        export_samples(
            runner_spec_path=runner_spec,
            output_dir=output_dir,
            shard_size=1,
            overwrite=True,
        )


def test_cli_dry_run_summary_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner_spec = _make_runner_fixture(tmp_path, sample_count=2)
    output_dir = tmp_path / "out"

    assert (
        main(
            [
                "--runner-spec",
                str(runner_spec),
                "--output-dir",
                str(output_dir),
                "--shard-size",
                "1",
                "--dry-run",
                "--summary",
                "json",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert summary["dry_run"] is True
    assert summary["total_samples"] == 2
    assert summary["shard_count"] == 2
    assert not output_dir.exists()
