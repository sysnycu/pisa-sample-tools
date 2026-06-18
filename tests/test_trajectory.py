from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import pytest
import yaml

from pisa_sample_tools.trajectory import (
    AgentState,
    TrajectoryError,
    discover_agent_state_files,
    load_agent_states,
    origin_for_agent,
    states_to_svg,
    translate_states,
    visualize_trajectories,
)
from pisa_sample_tools.trajectory_cli import main


def _write_agent_states(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "step_index",
                "sim_time_ms",
                "agent_id",
                "x",
                "y",
                "z",
                "yaw",
                "speed",
                "acceleration",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def _write_result(path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "run.status",
                "run.test_outcome",
                "run.params",
                "run.stop_reason",
                "ego.max_speed_mps",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "run.status": "finished",
                "run.test_outcome": "success",
                "run.params": json.dumps({"Ego_Speed": 10, "Agent_Speed": 15.5}),
                "run.stop_reason": "destination reached",
                "ego.max_speed_mps": "9.8",
            }
        )


def _rows(offset: float = 0.0) -> list[dict[str, object]]:
    return [
        {
            "step_index": 0,
            "sim_time_ms": 0,
            "agent_id": 0,
            "x": offset,
            "y": 0,
            "z": 0,
            "yaw": 0,
            "speed": 1,
            "acceleration": 0,
        },
        {
            "step_index": 1,
            "sim_time_ms": 100,
            "agent_id": 0,
            "x": offset + 10,
            "y": 0,
            "z": 0,
            "yaw": 0,
            "speed": 10,
            "acceleration": 0,
        },
        {
            "step_index": 0,
            "sim_time_ms": 0,
            "agent_id": 1,
            "x": offset,
            "y": 5,
            "z": 0,
            "yaw": 0,
            "speed": 4,
            "acceleration": 0,
        },
        {
            "step_index": 1,
            "sim_time_ms": 100,
            "agent_id": 1,
            "x": offset + 10,
            "y": 7,
            "z": 0,
            "yaw": 0,
            "speed": 7,
            "acceleration": 0,
        },
    ]


def test_load_agent_states_parses_and_sorts_rows(tmp_path: Path) -> None:
    csv_path = tmp_path / "agent_states.csv"
    _write_agent_states(csv_path, list(reversed(_rows())))

    states = load_agent_states(csv_path)

    assert [state.agent_id for state in states] == ["0", "0", "1", "1"]
    assert states[0].step_index == 0
    assert states[1].sim_time_ms == 100
    assert states[1].speed == 10


def test_visualize_single_iteration_writes_svg_with_agent_legend_and_speed_opacity(
    tmp_path: Path,
) -> None:
    iteration_dir = tmp_path / "results" / "iteration_1"
    _write_agent_states(iteration_dir / "monitor" / "agent_states.csv", _rows())
    _write_result(iteration_dir / "monitor" / "result.csv")
    output_dir = tmp_path / "trajectories"

    result = visualize_trajectories(input_path=iteration_dir, output_dir=output_dir)

    assert len(result.results) == 1
    svg_path = output_dir / "iteration_1_trajectory.svg"
    svg = svg_path.read_text(encoding="utf-8")
    assert "<svg" in svg
    assert "Trajectory: iteration_1" in svg
    assert "agent 0" in svg
    assert "agent 1" in svg
    assert "#2563eb" in svg
    assert "#16a34a" in svg
    assert "stroke-opacity=" in svg
    assert "Params" in svg
    assert "Ego_Speed: 10" in svg
    assert "Agent_Speed: 15.5" in svg
    assert "Result" in svg
    assert "status: finished" in svg
    assert "test_outcome: success" in svg
    assert "ego.max_speed_mps: 9.8" in svg
    assert result.results[0].agent_count == 2
    assert result.results[0].state_count == 4
    assert result.results[0].params == {"Ego_Speed": 10, "Agent_Speed": 15.5}
    assert result.results[0].result["status"] == "finished"
    assert result.results[0].result["ego.max_speed_mps"] == "9.8"


def test_visualize_results_folder_writes_all_iteration_svgs_and_manifest(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    _write_agent_states(results_dir / "iteration_1" / "monitor" / "agent_states.csv", _rows())
    _write_agent_states(results_dir / "iteration_2" / "monitor" / "agent_states.csv", _rows(20))
    output_dir = tmp_path / "trajectories"

    result = visualize_trajectories(input_path=results_dir, output_dir=output_dir)

    assert len(result.results) == 2
    assert (output_dir / "iteration_1_trajectory.svg").exists()
    assert (output_dir / "iteration_2_trajectory.svg").exists()
    manifest = yaml.safe_load((output_dir / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["svg_count"] == 2
    assert manifest["ignore_agent_ids"] == []
    assert manifest["origin_agent_id"] is None
    assert manifest["outputs"][0]["agent_count"] == 2
    assert manifest["outputs"][0]["state_count"] == 4


def test_visualize_manifest_includes_params_and_result(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    monitor_dir = results_dir / "iteration_1" / "monitor"
    _write_agent_states(monitor_dir / "agent_states.csv", _rows())
    _write_result(monitor_dir / "result.csv")
    output_dir = tmp_path / "trajectories"

    visualize_trajectories(input_path=results_dir, output_dir=output_dir)

    manifest = yaml.safe_load((output_dir / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["outputs"][0]["params"] == {"Ego_Speed": 10, "Agent_Speed": 15.5}
    assert manifest["outputs"][0]["result"]["status"] == "finished"
    assert manifest["outputs"][0]["result"]["test_outcome"] == "success"
    assert manifest["outputs"][0]["result"]["ego.max_speed_mps"] == "9.8"


def test_visualize_filters_points_by_xy_range(tmp_path: Path) -> None:
    iteration_dir = tmp_path / "results" / "iteration_1"
    _write_agent_states(iteration_dir / "monitor" / "agent_states.csv", _rows())
    output_dir = tmp_path / "trajectories"

    result = visualize_trajectories(
        input_path=iteration_dir,
        output_dir=output_dir,
        x_range=(-1, 1),
        y_range=(-1, 6),
    )

    svg = (output_dir / "iteration_1_trajectory.svg").read_text(encoding="utf-8")
    manifest = yaml.safe_load((output_dir / "manifest.yaml").read_text(encoding="utf-8"))
    assert result.results[0].state_count == 2
    assert "speed 1" in svg
    assert "speed 4" in svg
    assert "speed 10" not in svg
    assert manifest["x_range"] == [-1, 1]
    assert manifest["y_range"] == [-1, 6]


def test_visualize_can_ignore_agent_ids(tmp_path: Path) -> None:
    iteration_dir = tmp_path / "results" / "iteration_1"
    _write_agent_states(iteration_dir / "monitor" / "agent_states.csv", _rows())
    output_dir = tmp_path / "trajectories"

    result = visualize_trajectories(
        input_path=iteration_dir,
        output_dir=output_dir,
        ignore_agent_ids={"1"},
    )

    svg = (output_dir / "iteration_1_trajectory.svg").read_text(encoding="utf-8")
    manifest = yaml.safe_load((output_dir / "manifest.yaml").read_text(encoding="utf-8"))
    assert result.results[0].agent_count == 1
    assert result.results[0].state_count == 2
    assert "agent 0" in svg
    assert "agent 1" not in svg
    assert manifest["ignore_agent_ids"] == ["1"]


def test_visualize_can_translate_origin_to_agent_first_position(tmp_path: Path) -> None:
    iteration_dir = tmp_path / "results" / "iteration_1"
    _write_agent_states(iteration_dir / "monitor" / "agent_states.csv", _rows(offset=10))
    output_dir = tmp_path / "trajectories"

    result = visualize_trajectories(
        input_path=iteration_dir,
        output_dir=output_dir,
        origin_agent_id="1",
    )

    svg = (output_dir / "iteration_1_trajectory.svg").read_text(encoding="utf-8")
    manifest = yaml.safe_load((output_dir / "manifest.yaml").read_text(encoding="utf-8"))
    assert result.results[0].origin_agent_id == "1"
    assert result.results[0].origin_x == pytest.approx(10.0)
    assert result.results[0].origin_y == pytest.approx(5.0)
    assert manifest["origin_agent_id"] == "1"
    assert manifest["outputs"][0]["origin_x"] == pytest.approx(10.0)
    assert manifest["outputs"][0]["origin_y"] == pytest.approx(5.0)
    assert "agent 1 start" in svg


def test_translate_states_moves_origin_agent_first_position_to_zero() -> None:
    states = load_agent_states_from_rows(_rows(offset=10))
    origin = origin_for_agent(states, "1")

    assert origin == pytest.approx((10.0, 5.0))
    translated = translate_states(states, origin=origin)
    agent_1_start = next(state for state in translated if state.agent_id == "1" and state.step_index == 0)
    agent_0_start = next(state for state in translated if state.agent_id == "0" and state.step_index == 0)
    assert agent_1_start.x == pytest.approx(0.0)
    assert agent_1_start.y == pytest.approx(0.0)
    assert agent_0_start.x == pytest.approx(0.0)
    assert agent_0_start.y == pytest.approx(-5.0)


def test_visualize_can_use_ignored_agent_as_origin(tmp_path: Path) -> None:
    iteration_dir = tmp_path / "results" / "iteration_1"
    _write_agent_states(iteration_dir / "monitor" / "agent_states.csv", _rows(offset=10))
    output_dir = tmp_path / "trajectories"

    result = visualize_trajectories(
        input_path=iteration_dir,
        output_dir=output_dir,
        ignore_agent_ids={"1"},
        origin_agent_id="1",
    )

    svg = (output_dir / "iteration_1_trajectory.svg").read_text(encoding="utf-8")
    manifest = yaml.safe_load((output_dir / "manifest.yaml").read_text(encoding="utf-8"))
    assert result.results[0].agent_count == 1
    assert "agent 1" not in svg
    assert manifest["ignore_agent_ids"] == ["1"]
    assert manifest["origin_agent_id"] == "1"


def test_visualize_origin_agent_id_must_exist(tmp_path: Path) -> None:
    iteration_dir = tmp_path / "results" / "iteration_1"
    _write_agent_states(iteration_dir / "monitor" / "agent_states.csv", _rows())
    output_dir = tmp_path / "trajectories"

    with pytest.raises(TrajectoryError, match="origin agent id not found"):
        visualize_trajectories(
            input_path=iteration_dir,
            output_dir=output_dir,
            origin_agent_id="missing",
        )


def test_states_to_svg_uses_equal_xy_scale() -> None:
    states = load_agent_states_from_rows(_rows())

    svg = states_to_svg(
        states,
        title="equal scale",
        x_range=(0, 100),
        y_range=(0, 10),
    )

    rect = re.search(
        r'id="trajectory-plot-area"[^>]*width="(?P<width>[0-9.]+)"[^>]*height="(?P<height>[0-9.]+)"',
        svg,
    )
    assert rect is not None
    plot_width = float(rect.group("width"))
    plot_height = float(rect.group("height"))
    assert plot_width / plot_height == pytest.approx(10.0)


def test_states_to_svg_can_stretch_xy_scale() -> None:
    states = load_agent_states_from_rows(_rows())

    svg = states_to_svg(
        states,
        title="stretched scale",
        x_range=(0, 100),
        y_range=(0, 10),
        equal_scale=False,
    )

    rect = re.search(
        r'id="trajectory-plot-area"[^>]*width="(?P<width>[0-9.]+)"[^>]*height="(?P<height>[0-9.]+)"',
        svg,
    )
    assert rect is not None
    plot_width = float(rect.group("width"))
    plot_height = float(rect.group("height"))
    assert plot_width / plot_height != pytest.approx(10.0)
    assert plot_width == pytest.approx(738.0)
    assert plot_height == pytest.approx(618.0)


def test_discover_supports_singular_agent_state_filename(tmp_path: Path) -> None:
    path = tmp_path / "results" / "iteration_1" / "monitor" / "agent_state.csv"
    _write_agent_states(path, _rows())

    assert discover_agent_state_files(tmp_path / "results") == [path]


def test_visualize_rejects_existing_output_without_overwrite(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    _write_agent_states(results_dir / "iteration_1" / "monitor" / "agent_states.csv", _rows())
    output_dir = tmp_path / "trajectories"
    output_dir.mkdir()

    with pytest.raises(TrajectoryError, match="already exists"):
        visualize_trajectories(input_path=results_dir, output_dir=output_dir)


def test_visualize_overwrite_replaces_previous_tool_output(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    _write_agent_states(results_dir / "iteration_1" / "monitor" / "agent_states.csv", _rows())
    output_dir = tmp_path / "trajectories"

    visualize_trajectories(input_path=results_dir, output_dir=output_dir)
    _write_agent_states(results_dir / "iteration_2" / "monitor" / "agent_states.csv", _rows(20))
    result = visualize_trajectories(input_path=results_dir, output_dir=output_dir, overwrite=True)

    assert len(result.results) == 2
    assert (output_dir / "iteration_1_trajectory.svg").exists()
    assert (output_dir / "iteration_2_trajectory.svg").exists()


def test_cli_trajectory_writes_batch_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    results_dir = tmp_path / "results"
    _write_agent_states(results_dir / "iteration_1" / "monitor" / "agent_states.csv", _rows())
    output_dir = tmp_path / "trajectories"

    assert main(["--input", str(results_dir), "--output-dir", str(output_dir)]) == 0

    captured = capsys.readouterr()
    assert "svg_count: 1" in captured.out
    assert (output_dir / "iteration_1_trajectory.svg").exists()


def test_cli_trajectory_accepts_xy_range(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    _write_agent_states(results_dir / "iteration_1" / "monitor" / "agent_states.csv", _rows())
    output_dir = tmp_path / "trajectories"

    assert (
        main(
            [
                "--input",
                str(results_dir),
                "--output-dir",
                str(output_dir),
                "--x-range",
                "-1,1",
                "--y-range",
                "-1,6",
            ]
        )
        == 0
    )

    manifest = yaml.safe_load((output_dir / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["x_range"] == [-1, 1]
    assert manifest["y_range"] == [-1, 6]
    assert manifest["outputs"][0]["state_count"] == 2


def test_cli_trajectory_accepts_stretch_scale_mode(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    _write_agent_states(results_dir / "iteration_1" / "monitor" / "agent_states.csv", _rows())
    output_dir = tmp_path / "trajectories"

    assert (
        main(
            [
                "--input",
                str(results_dir),
                "--output-dir",
                str(output_dir),
                "--x-range",
                "0,100",
                "--y-range",
                "0,10",
                "--scale-mode",
                "stretch",
            ]
        )
        == 0
    )

    manifest = yaml.safe_load((output_dir / "manifest.yaml").read_text(encoding="utf-8"))
    svg = (output_dir / "iteration_1_trajectory.svg").read_text(encoding="utf-8")
    assert manifest["scale_mode"] == "stretch"
    rect = re.search(
        r'id="trajectory-plot-area"[^>]*width="(?P<width>[0-9.]+)"[^>]*height="(?P<height>[0-9.]+)"',
        svg,
    )
    assert rect is not None
    assert float(rect.group("width")) / float(rect.group("height")) != pytest.approx(10.0)


def test_cli_trajectory_accepts_ignored_agent_ids(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    _write_agent_states(results_dir / "iteration_1" / "monitor" / "agent_states.csv", _rows())
    output_dir = tmp_path / "trajectories"

    assert (
        main(
            [
                "--input",
                str(results_dir),
                "--output-dir",
                str(output_dir),
                "--ignore-agent-id",
                "1",
            ]
        )
        == 0
    )

    manifest = yaml.safe_load((output_dir / "manifest.yaml").read_text(encoding="utf-8"))
    svg = (output_dir / "iteration_1_trajectory.svg").read_text(encoding="utf-8")
    assert manifest["ignore_agent_ids"] == ["1"]
    assert manifest["outputs"][0]["agent_count"] == 1
    assert "agent 1" not in svg


def test_cli_trajectory_accepts_origin_agent_id(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    _write_agent_states(results_dir / "iteration_1" / "monitor" / "agent_states.csv", _rows(offset=10))
    output_dir = tmp_path / "trajectories"

    assert (
        main(
            [
                "--input",
                str(results_dir),
                "--output-dir",
                str(output_dir),
                "--origin-agent-id",
                "1",
            ]
        )
        == 0
    )

    manifest = yaml.safe_load((output_dir / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["origin_agent_id"] == "1"
    assert manifest["outputs"][0]["origin_x"] == pytest.approx(10.0)
    assert manifest["outputs"][0]["origin_y"] == pytest.approx(5.0)


def load_agent_states_from_rows(rows: list[dict[str, object]]) -> list[AgentState]:
    return sorted(
        (_row_to_state(row) for row in rows),
        key=lambda state: (state.agent_id, state.step_index or 0),
    )


def _row_to_state(row: dict[str, object]) -> AgentState:
    return AgentState(
        step_index=int(row["step_index"]),
        sim_time_ms=float(row["sim_time_ms"]),
        agent_id=str(row["agent_id"]),
        x=float(row["x"]),
        y=float(row["y"]),
        speed=float(row["speed"]),
    )
