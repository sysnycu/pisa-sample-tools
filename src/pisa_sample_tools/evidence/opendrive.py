from __future__ import annotations

import hashlib
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


def discover_xodr(result_path: Path, metadata: dict[str, Any]) -> Path | None:
    explicit = metadata.get("xodr_path")
    if explicit:
        path = Path(str(explicit)).expanduser()
        if path.is_file():
            return path.resolve()
        if path.is_dir():
            files = sorted(path.glob("*.xodr"))
            map_name = str(metadata.get("map_name") or "")
            named = [item for item in files if item.stem == map_name]
            if len(named) == 1:
                return named[0].resolve()
            if len(files) == 1:
                return files[0].resolve()
        return None
    map_name = str(metadata.get("map_name") or "")
    for ancestor in (result_path, *result_path.parents):
        candidates = []
        if map_name:
            candidates.extend(
                [
                    ancestor / "map" / map_name / "xodr" / f"{map_name}.xodr",
                    ancestor / "maps" / f"{map_name}.xodr",
                    ancestor / f"{map_name}.xodr",
                ]
            )
        candidates.extend(sorted((ancestor / "map").glob("**/*.xodr")) if (ancestor / "map").is_dir() else [])
        existing = [path for path in candidates if path.is_file()]
        if len(existing) == 1:
            return existing[0].resolve()
        if map_name:
            named = [path for path in existing if path.stem == map_name]
            if len(named) == 1:
                return named[0].resolve()
    return None


def load_map_geometry(path: Path, *, step_m: float = 2.0) -> tuple[dict[str, Any] | None, str | None]:
    try:
        root = ET.parse(path).getroot()
        roads = [_road_geometry(road, step_m) for road in root.findall("road")]
        roads = [road for road in roads if road["reference_line"]]
        return {
            "source": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "roads": roads,
            "road_count": len(roads),
            "step_m": step_m,
        }, None
    except (OSError, ET.ParseError, ValueError) as exc:
        return None, f"OpenDRIVE map could not be parsed: {exc}"


def _road_geometry(road: ET.Element, step_m: float) -> dict[str, Any]:
    points: list[list[float]] = []
    road_positions: list[float] = []
    for geometry in road.findall("./planView/geometry"):
        length = float(geometry.get("length", "0"))
        start_s = float(geometry.get("s", road_positions[-1] if road_positions else "0"))
        count = max(2, math.ceil(length / step_m) + 1)
        for index in range(count):
            ds = length * index / (count - 1)
            point = _geometry_point(geometry, ds)
            if not points or point != points[-1]:
                points.append(point)
                road_positions.append(start_s + ds)
    boundaries = _lane_boundary_lines(road, points, road_positions)
    if not boundaries:
        left, right = _offset_lines(points, 3.5)
        boundaries = [left, right]
    return {
        "road_id": road.get("id"),
        "name": road.get("name") or "",
        "junction": road.get("junction") not in {None, "", "-1"},
        "reference_line": points,
        "boundaries": boundaries,
        "lane_boundary_count": len(boundaries),
    }


def _lane_boundary_lines(
    road: ET.Element, points: list[list[float]], road_positions: list[float]
) -> list[list[list[float]]]:
    sections = sorted(
        road.findall("./lanes/laneSection"), key=lambda item: float(item.get("s", "0"))
    )
    if not sections or len(points) != len(road_positions):
        return []
    lines: dict[tuple[str, str], list[list[float]]] = {}
    for point_index, (point, road_s) in enumerate(zip(points, road_positions, strict=True)):
        section = max(
            (item for item in sections if float(item.get("s", "0")) <= road_s),
            key=lambda item: float(item.get("s", "0")),
            default=sections[0],
        )
        section_s = float(section.get("s", "0"))
        before, after = points[max(0, point_index - 1)], points[min(len(points) - 1, point_index + 1)]
        heading = math.atan2(after[1] - before[1], after[0] - before[0])
        for side, sign in (("left", 1.0), ("right", -1.0)):
            cumulative = 0.0
            lanes = sorted(
                section.findall(f"./{side}/lane"),
                key=lambda lane: abs(int(lane.get("id", "0"))),
            )
            for lane in lanes:
                cumulative += _lane_width(lane, max(0.0, road_s - section_s))
                if cumulative <= 0:
                    continue
                key = (side, str(lane.get("id") or len(lines)))
                offset = sign * cumulative
                lines.setdefault(key, []).append(
                    [
                        round(point[0] - math.sin(heading) * offset, 6),
                        round(point[1] + math.cos(heading) * offset, 6),
                    ]
                )
    return [line for line in lines.values() if len(line) >= 2]


def _lane_width(lane: ET.Element, section_offset: float) -> float:
    records = sorted(lane.findall("width"), key=lambda item: float(item.get("sOffset", "0")))
    if not records:
        return 0.0
    record = max(
        (item for item in records if float(item.get("sOffset", "0")) <= section_offset),
        key=lambda item: float(item.get("sOffset", "0")),
        default=records[0],
    )
    ds = max(0.0, section_offset - float(record.get("sOffset", "0")))
    a, b, c, d = (float(record.get(key, "0")) for key in ("a", "b", "c", "d"))
    return abs(a + b * ds + c * ds**2 + d * ds**3)


def _geometry_point(geometry: ET.Element, ds: float) -> list[float]:
    x, y = float(geometry.get("x", "0")), float(geometry.get("y", "0"))
    heading = float(geometry.get("hdg", "0"))
    if geometry.find("line") is not None:
        return [round(x + ds * math.cos(heading), 6), round(y + ds * math.sin(heading), 6)]
    arc = geometry.find("arc")
    if arc is not None:
        curvature = float(arc.get("curvature", "0"))
        if math.isclose(curvature, 0):
            return [round(x + ds * math.cos(heading), 6), round(y + ds * math.sin(heading), 6)]
        angle = heading + curvature * ds
        return [
            round(x + (math.sin(angle) - math.sin(heading)) / curvature, 6),
            round(y - (math.cos(angle) - math.cos(heading)) / curvature, 6),
        ]
    spiral = geometry.find("spiral")
    if spiral is not None:
        length = float(geometry.get("length", "0"))
        start = float(spiral.get("curvStart", "0"))
        end = float(spiral.get("curvEnd", "0"))
        count = max(1, math.ceil(ds / 0.25))
        step = ds / count
        px, py, angle = x, y, heading
        for index in range(count):
            midpoint = (index + 0.5) * step
            curvature = start + (end - start) * midpoint / length if length else start
            mid_angle = angle + curvature * step / 2
            px += step * math.cos(mid_angle)
            py += step * math.sin(mid_angle)
            angle += curvature * step
        return [round(px, 6), round(py, 6)]
    poly = geometry.find("poly3")
    if poly is not None:
        a, b, c, d = (float(poly.get(key, "0")) for key in ("a", "b", "c", "d"))
        u, v = ds, a + b * ds + c * ds**2 + d * ds**3
        return [round(x + u * math.cos(heading) - v * math.sin(heading), 6), round(y + u * math.sin(heading) + v * math.cos(heading), 6)]
    param = geometry.find("paramPoly3")
    if param is not None:
        length = float(geometry.get("length", "0"))
        p = ds / length if param.get("pRange") == "normalized" and length else ds
        u = float(param.get("aU", "0")) + float(param.get("bU", "0")) * p + float(param.get("cU", "0")) * p**2 + float(param.get("dU", "0")) * p**3
        v = float(param.get("aV", "0")) + float(param.get("bV", "0")) * p + float(param.get("cV", "0")) * p**2 + float(param.get("dV", "0")) * p**3
        return [round(x + u * math.cos(heading) - v * math.sin(heading), 6), round(y + u * math.sin(heading) + v * math.cos(heading), 6)]
    raise ValueError("unsupported OpenDRIVE planView geometry")


def _offset_lines(points: list[list[float]], distance: float) -> tuple[list[list[float]], list[list[float]]]:
    left, right = [], []
    for index, point in enumerate(points):
        before, after = points[max(0, index - 1)], points[min(len(points) - 1, index + 1)]
        heading = math.atan2(after[1] - before[1], after[0] - before[0])
        dx, dy = -math.sin(heading) * distance, math.cos(heading) * distance
        left.append([round(point[0] + dx, 6), round(point[1] + dy, 6)])
        right.append([round(point[0] - dx, 6), round(point[1] - dy, 6)])
    return left, right
