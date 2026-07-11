from pathlib import Path

from pisa_sample_tools.evidence.opendrive import discover_xodr, load_map_geometry


def _write_map(path: Path, geometry: str = "<line/>") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""<OpenDRIVE><road id="1" name="main" length="10" junction="-1">
        <planView><geometry s="0" x="0" y="0" hdg="0" length="10">{geometry}</geometry></planView>
        <lanes><laneSection s="0"><left><lane id="1"><width a="3.5" b="0" c="0" d="0"/></lane></left><right><lane id="-1"><width a="3.5" b="0" c="0" d="0"/></lane></right></laneSection></lanes>
        </road></OpenDRIVE>""",
        encoding="utf-8",
    )


def test_load_map_geometry_samples_reference_and_lane_boundaries(tmp_path: Path) -> None:
    path = tmp_path / "road.xodr"
    _write_map(path)

    geometry, warning = load_map_geometry(path, step_m=2)

    assert warning is None
    assert geometry is not None
    assert geometry["road_count"] == 1
    assert geometry["roads"][0]["reference_line"] == [
        [0.0, 0.0],
        [2.0, 0.0],
        [4.0, 0.0],
        [6.0, 0.0],
        [8.0, 0.0],
        [10.0, 0.0],
    ]
    assert geometry["roads"][0]["boundaries"][0][0] == [0.0, 3.5]
    assert len(geometry["sha256"]) == 64


def test_load_map_geometry_supports_arc_and_spiral(tmp_path: Path) -> None:
    arc = tmp_path / "arc.xodr"
    spiral = tmp_path / "spiral.xodr"
    _write_map(arc, '<arc curvature="0.1"/>')
    _write_map(spiral, '<spiral curvStart="0" curvEnd="0.1"/>')

    arc_geometry, arc_warning = load_map_geometry(arc)
    spiral_geometry, spiral_warning = load_map_geometry(spiral)

    assert arc_warning is None and arc_geometry is not None
    assert spiral_warning is None and spiral_geometry is not None
    assert arc_geometry["roads"][0]["reference_line"][-1][1] > 0
    assert spiral_geometry["roads"][0]["reference_line"][-1][1] > 0


def test_discover_xodr_prefers_explicit_campaign_path(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit.xodr"
    _write_map(explicit)

    assert discover_xodr(tmp_path / "results" / "iteration_1", {"xodr_path": explicit}) == explicit


def test_discover_xodr_resolves_modern_manifest_directory(tmp_path: Path) -> None:
    explicit = tmp_path / "xodr" / "town.xodr"
    _write_map(explicit)

    assert discover_xodr(
        tmp_path / "results" / "iteration_1",
        {"xodr_path": explicit.parent, "map_name": "town"},
    ) == explicit
