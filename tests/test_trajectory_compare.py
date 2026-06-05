from __future__ import annotations

import csv
from pathlib import Path

import pytest
import yaml

from pisa_sample_tools.trajectory import AgentState
from pisa_sample_tools.trajectory_compare import (
    TrajectoryCompareError,
    compare_states,
    compare_trajectory_sets,
)
from pisa_sample_tools.trajectory_compare_cli import main


def _write_agent_states(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["step_index", "sim_time_ms", "agent_id", "x", "y", "speed"],
        )
        writer.writeheader()
        writer.writerows(rows)


def _left_rows(offset: float = 0.0) -> list[dict[str, object]]:
    return [
        {"step_index": 0, "sim_time_ms": 0, "agent_id": 0, "x": offset + 0, "y": 0, "speed": 10},
        {"step_index": 1, "sim_time_ms": 50, "agent_id": 0, "x": offset + 1, "y": 0, "speed": 10},
        {"step_index": 2, "sim_time_ms": 100, "agent_id": 0, "x": offset + 2, "y": 0, "speed": 10},
        {"step_index": 0, "sim_time_ms": 0, "agent_id": 1, "x": offset + 100, "y": 0, "speed": 20},
        {"step_index": 1, "sim_time_ms": 50, "agent_id": 1, "x": offset + 101, "y": 0, "speed": 20},
    ]


def _right_rows(offset: float = 0.0) -> list[dict[str, object]]:
    return [
        {"step_index": 0, "sim_time_ms": 0, "agent_id": 0, "x": offset + 0, "y": 1, "speed": 11},
        {"step_index": 1, "sim_time_ms": 50, "agent_id": 0, "x": offset + 1, "y": 1, "speed": 11},
        {"step_index": 0, "sim_time_ms": 0, "agent_id": 1, "x": offset + 100, "y": 100, "speed": 20},
        {"step_index": 1, "sim_time_ms": 50, "agent_id": 1, "x": offset + 101, "y": 100, "speed": 20},
    ]


def test_compare_states_ignores_ego_and_truncates_to_shorter_timestep() -> None:
    left = [
        AgentState(step_index=0, sim_time_ms=0, agent_id="0", x=0, y=0, speed=10),
        AgentState(step_index=1, sim_time_ms=50, agent_id="0", x=1, y=0, speed=10),
        AgentState(step_index=2, sim_time_ms=100, agent_id="0", x=2, y=0, speed=10),
        AgentState(step_index=0, sim_time_ms=0, agent_id="1", x=0, y=0, speed=10),
    ]
    right = [
        AgentState(step_index=0, sim_time_ms=0, agent_id="0", x=0, y=1, speed=12),
        AgentState(step_index=1, sim_time_ms=50, agent_id="0", x=1, y=1, speed=12),
        AgentState(step_index=0, sim_time_ms=0, agent_id="1", x=0, y=100, speed=10),
    ]

    comparisons = compare_states(left, right, ignore_agent_ids={"1"})

    assert len(comparisons) == 1
    agent = comparisons[0]
    assert agent.agent_id == "0"
    assert agent.compared_steps == 2
    assert agent.ade == pytest.approx(1.0)
    assert agent.fde == pytest.approx(1.0)
    assert agent.rmse == pytest.approx(1.0)
    assert agent.max_error == pytest.approx(1.0)
    assert agent.mean_speed_delta == pytest.approx(2.0)


def test_compare_single_iteration_writes_svg_summary_and_manifest(tmp_path: Path) -> None:
    left = tmp_path / "left" / "iteration_1"
    right = tmp_path / "right" / "iteration_1"
    _write_agent_states(left / "monitor" / "agent_states.csv", _left_rows())
    _write_agent_states(right / "monitor" / "agent_states.csv", _right_rows())
    output_dir = tmp_path / "compare"

    result = compare_trajectory_sets(left_path=left, right_path=right, output_dir=output_dir)

    assert len(result.comparisons) == 1
    assert result.comparisons[0].ade == pytest.approx(1.0)
    assert (output_dir / "iteration_1_comparison.svg").exists()
    assert (output_dir / "summary.csv").exists()
    manifest = yaml.safe_load((output_dir / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["comparison_count"] == 1
    assert manifest["ignore_agent_ids"] == ["1"]
    assert manifest["comparisons"][0]["agents"][0]["agent_id"] == "0"
    svg = (output_dir / "iteration_1_comparison.svg").read_text(encoding="utf-8")
    assert "ADE 1" in svg
    assert "solid" in svg
    assert "dashed" in svg
    assert "ignored agents: 1" in svg


def test_compare_batch_pairs_shared_iterations(tmp_path: Path) -> None:
    left_root = tmp_path / "left"
    right_root = tmp_path / "right"
    _write_agent_states(left_root / "iteration_1" / "monitor" / "agent_states.csv", _left_rows())
    _write_agent_states(left_root / "iteration_2" / "monitor" / "agent_states.csv", _left_rows(10))
    _write_agent_states(right_root / "iteration_1" / "monitor" / "agent_states.csv", _right_rows())
    _write_agent_states(right_root / "iteration_3" / "monitor" / "agent_states.csv", _right_rows(20))
    output_dir = tmp_path / "compare"

    result = compare_trajectory_sets(left_path=left_root, right_path=right_root, output_dir=output_dir)

    assert [comparison.name for comparison in result.comparisons] == ["iteration_1"]
    assert (output_dir / "iteration_1_comparison.svg").exists()
    assert not (output_dir / "iteration_2_comparison.svg").exists()


def test_compare_overwrite_replaces_previous_tool_output(tmp_path: Path) -> None:
    left = tmp_path / "left" / "iteration_1"
    right = tmp_path / "right" / "iteration_1"
    _write_agent_states(left / "monitor" / "agent_states.csv", _left_rows())
    _write_agent_states(right / "monitor" / "agent_states.csv", _right_rows())
    output_dir = tmp_path / "compare"

    compare_trajectory_sets(left_path=left, right_path=right, output_dir=output_dir)
    stale_svg = output_dir / "iteration_1_comparison.svg"
    stale_svg.write_text("stale", encoding="utf-8")

    compare_trajectory_sets(left_path=left, right_path=right, output_dir=output_dir, overwrite=True)

    assert "stale" not in stale_svg.read_text(encoding="utf-8")
    assert (output_dir / "summary.csv").exists()


def test_compare_overwrite_rejects_non_tool_output(tmp_path: Path) -> None:
    left = tmp_path / "left" / "iteration_1"
    right = tmp_path / "right" / "iteration_1"
    _write_agent_states(left / "monitor" / "agent_states.csv", _left_rows())
    _write_agent_states(right / "monitor" / "agent_states.csv", _right_rows())
    output_dir = tmp_path / "compare"
    output_dir.mkdir()
    (output_dir / "notes.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(TrajectoryCompareError, match="refusing to overwrite non-tool output"):
        compare_trajectory_sets(left_path=left, right_path=right, output_dir=output_dir, overwrite=True)


def test_compare_skips_pairs_without_non_ego_overlap_without_writing_svg(tmp_path: Path) -> None:
    left = tmp_path / "left" / "iteration_1"
    right = tmp_path / "right" / "iteration_1"
    _write_agent_states(
        left / "monitor" / "agent_states.csv",
        [{"step_index": 0, "sim_time_ms": 0, "agent_id": 1, "x": 0, "y": 0, "speed": 10}],
    )
    _write_agent_states(
        right / "monitor" / "agent_states.csv",
        [{"step_index": 0, "sim_time_ms": 0, "agent_id": 1, "x": 10, "y": 10, "speed": 10}],
    )
    output_dir = tmp_path / "compare"

    with pytest.raises(TrajectoryCompareError, match="no non-ignored agents overlapped"):
        compare_trajectory_sets(left_path=left, right_path=right, output_dir=output_dir)

    assert not (output_dir / "iteration_1_comparison.svg").exists()


def test_cli_compare(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    left = tmp_path / "left" / "iteration_1"
    right = tmp_path / "right" / "iteration_1"
    _write_agent_states(left / "monitor" / "agent_states.csv", _left_rows())
    _write_agent_states(right / "monitor" / "agent_states.csv", _right_rows())
    output_dir = tmp_path / "compare"

    assert (
        main(
            [
                "--left",
                str(left),
                "--right",
                str(right),
                "--output-dir",
                str(output_dir),
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert "comparison_count: 1" in captured.out
    assert (output_dir / "summary.csv").exists()
