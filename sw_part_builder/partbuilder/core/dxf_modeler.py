"""Best-effort preliminary solid generation from dimensioned orthographic DXFs."""
from dataclasses import dataclass
import math
from pathlib import Path
from statistics import median
from typing import Any

from .dxf_dimensions import analyze_dxf_drawing
from .model import Circle, Extrude, Part, Polygon, Sketch


class PreliminaryModelError(ValueError):
    """A DXF does not contain enough evidence for a preliminary solid."""


@dataclass
class PreliminaryModel:
    part: Part
    source_region: str
    drawing_scale: float
    profile_width_mm: float
    profile_height_mm: float
    thickness_mm: float
    profile_entity_handles: list[str]
    hole_count: int
    hole_diameter_mm: float | None
    countersink_diameter_mm: float | None
    review_note: str


def _alignment_ratio(region: dict[str, Any], entities: dict[str, Any]) -> float:
    total = aligned = 0
    for handle in region["entity_handles"]:
        entity = entities.get(handle)
        if entity is None or entity.dxftype() != "LINE":
            continue
        start, end = entity.dxf.start, entity.dxf.end
        dx, dy = abs(end.x - start.x), abs(end.y - start.y)
        length = math.hypot(dx, dy)
        if length <= 0.2:
            continue
        total += 1
        if min(dx, dy) <= max(length * 0.02, 0.02):
            aligned += 1
    return aligned / total if total else 0.0


def _dimension_match(
    region: dict[str, Any],
    dimensions: list[dict[str, Any]],
) -> tuple[float, float] | None:
    linear_values = sorted({
        float(dimension["value"])
        for dimension in dimensions
        if dimension.get("type") in ("linear", "aligned")
        and float(dimension.get("value", 0)) > 0
    })
    sizes = [float(region["width"]), float(region["height"])]
    best: tuple[float, float] | None = None
    for first_index, first_size in enumerate(sizes):
        for second_index, second_size in enumerate(sizes):
            if first_index == second_index:
                continue
            for first_value in linear_values:
                first_scale = first_value / first_size
                for second_value in linear_values:
                    if second_value == first_value:
                        continue
                    second_scale = second_value / second_size
                    scale = (first_scale + second_scale) / 2
                    mismatch = abs(first_scale - second_scale) / scale
                    if scale > 0 and mismatch <= 0.12 and (
                        best is None or mismatch < best[0]
                    ):
                        best = (mismatch, scale)
    return best


def _entity_points(entity: Any, flattening_distance: float) -> list[tuple[float, float]]:
    from ezdxf.path import make_path

    points = [
        (float(point.x), float(point.y))
        for point in make_path(entity).flattening(
            distance=flattening_distance,
            segments=8,
        )
    ]
    return points


def _profile_cycles(
    region: dict[str, Any],
    entities: dict[str, Any],
) -> list[tuple[float, list[tuple[float, float]], list[str]]]:
    tolerance = 0.05
    edges = []
    adjacency: dict[tuple[int, int], list[tuple[int, tuple[int, int]]]] = {}
    for handle in region["entity_handles"]:
        entity = entities.get(handle)
        if entity is None or entity.dxftype() == "CIRCLE":
            continue
        try:
            points = _entity_points(entity, flattening_distance=0.1)
        except (ArithmeticError, AttributeError, TypeError, ValueError):
            continue
        if len(points) < 2 or sum(
            math.dist(left, right) for left, right in zip(points, points[1:])
        ) < 0.25:
            continue
        start = tuple(round(value / tolerance) for value in points[0])
        end = tuple(round(value / tolerance) for value in points[-1])
        if start == end:
            continue
        edge_index = len(edges)
        edges.append((handle, points, start, end))
        adjacency.setdefault(start, []).append((edge_index, end))
        adjacency.setdefault(end, []).append((edge_index, start))

    cycles = {}

    def visit(
        start: tuple[int, int],
        node: tuple[int, int],
        visited_nodes: set[tuple[int, int]],
        used_edges: set[int],
        traversals: list[tuple[int, tuple[int, int], tuple[int, int]]],
    ) -> None:
        for edge_index, neighbor in adjacency.get(node, []):
            if edge_index in used_edges:
                continue
            traversal = (edge_index, node, neighbor)
            if neighbor == start and len(traversals) >= 2:
                cycle_traversals = [*traversals, traversal]
                polygon = []
                handles = []
                current = start
                for index, from_node, to_node in cycle_traversals:
                    handle, points, edge_start, edge_end = edges[index]
                    if edge_start == from_node and edge_end == to_node:
                        oriented = points
                    elif edge_end == from_node and edge_start == to_node:
                        oriented = list(reversed(points))
                    else:
                        break
                    polygon.extend(oriented[:-1])
                    handles.append(handle)
                    current = to_node
                if current != start or len(polygon) < 3:
                    continue
                area = abs(sum(
                    polygon[index][0] * polygon[(index + 1) % len(polygon)][1]
                    - polygon[(index + 1) % len(polygon)][0] * polygon[index][1]
                    for index in range(len(polygon))
                ) / 2)
                key = tuple(sorted(index for index, _, _ in cycle_traversals))
                cycles[key] = (area, polygon, handles)
            elif neighbor not in visited_nodes and len(traversals) < 12:
                visit(
                    start,
                    neighbor,
                    visited_nodes | {neighbor},
                    used_edges | {edge_index},
                    [*traversals, traversal],
                )

    for start in adjacency:
        visit(start, start, {start}, set(), [])

    return sorted(cycles.values(), key=lambda cycle: cycle[0], reverse=True)


def _simplify_closed_polygon(
    points: list[tuple[float, float]],
    tolerance: float = 0.05,
) -> list[tuple[float, float]]:
    simplified = list(points)
    changed = True
    while changed and len(simplified) > 3:
        changed = False
        for index, point in enumerate(simplified):
            previous = simplified[index - 1]
            following = simplified[(index + 1) % len(simplified)]
            segment_length = math.dist(previous, following)
            if segment_length == 0:
                distance = math.dist(point, previous)
            else:
                distance = abs(
                    (following[0] - previous[0]) * (previous[1] - point[1])
                    - (previous[0] - point[0]) * (following[1] - previous[1])
                ) / segment_length
            if distance <= tolerance:
                simplified.pop(index)
                changed = True
                break
    return simplified


def _infer_thickness(
    regions: list[dict[str, Any]],
    alignments: dict[str, float],
    main_region: dict[str, Any],
    scale: float,
    dimensions: list[dict[str, Any]],
) -> float:
    main_sizes = (main_region["width"] * scale, main_region["height"] * scale)
    candidates = []
    for region in regions:
        if region["candidate_id"] == main_region["candidate_id"]:
            continue
        if alignments.get(region["candidate_id"], 0) < 0.6:
            continue
        sizes = (region["width"] * scale, region["height"] * scale)
        if any(
            abs(size - main_size) / main_size <= 0.10
            for size in sizes
            for main_size in main_sizes
        ):
            candidates.append(min(sizes))
    if not candidates:
        raise PreliminaryModelError(
            "A separate orthographic view matching the selected profile was not "
            "found, so the extrusion thickness cannot be estimated."
        )
    thickness = float(median(candidates))
    matching_dimensions = [
        float(dimension["value"])
        for dimension in dimensions
        if dimension.get("type") in ("linear", "aligned")
        and float(dimension.get("value", 0)) > 0
        and abs(float(dimension["value"]) - thickness) / thickness <= 0.10
    ]
    if not matching_dimensions:
        raise PreliminaryModelError(
            f"The projected thickness estimate ({thickness:.3g} mm) has no "
            "nearby native linear dimension to support it."
        )
    return thickness


def _circle_groups(
    region: dict[str, Any],
    entities: dict[str, Any],
    tolerance: float = 0.1,
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for handle in region["entity_handles"]:
        entity = entities.get(handle)
        if entity is None or entity.dxftype() != "CIRCLE":
            continue
        center = (float(entity.dxf.center.x), float(entity.dxf.center.y))
        radius = float(entity.dxf.radius)
        group = next(
            (
                candidate
                for candidate in groups
                if math.dist(candidate["center"], center) <= tolerance
            ),
            None,
        )
        if group is None:
            groups.append({
                "center": center,
                "radii": [radius],
                "handles": [handle],
            })
        else:
            count = len(group["radii"])
            group["center"] = (
                (group["center"][0] * count + center[0]) / (count + 1),
                (group["center"][1] * count + center[1]) / (count + 1),
            )
            group["radii"].append(radius)
            group["handles"].append(handle)
    return groups


def _detect_orthographic_holes(
    regions: list[dict[str, Any]],
    entities: dict[str, Any],
    main_region: dict[str, Any],
    scale: float,
    thickness_mm: float,
    profile_min_y: float,
    dimensions: list[dict[str, Any]],
) -> tuple[list[tuple[str, float, float]], float, float | None] | None:
    """Match circular features in plan and end projections to the L-profile."""
    draw_thickness = thickness_mm / scale
    top_views = []
    end_views = []
    for region in regions:
        if region["candidate_id"] == main_region["candidate_id"]:
            continue
        circles = _circle_groups(region, entities)
        if len(circles) < 2:
            continue
        same_width = abs(region["width"] - main_region["width"]) / main_region["width"]
        same_height = abs(region["height"] - main_region["height"]) / main_region["height"]
        top_size_match = (
            same_width <= 0.10
            and abs(region["height"] - draw_thickness) / draw_thickness <= 0.20
        )
        end_size_match = (
            abs(region["width"] - draw_thickness) / draw_thickness <= 0.20
            and same_height <= 0.10
        )
        if top_size_match and all(len(group["radii"]) == 1 for group in circles):
            top_views.append((region, circles))
        if end_size_match and all(len(group["radii"]) >= 2 for group in circles):
            end_views.append((region, circles))
    if len(top_views) != 1 or len(end_views) != 1:
        return None

    top_region, top_circles = top_views[0]
    end_region, end_circles = end_views[0]
    if len(top_circles) != 2 or len(end_circles) != 2:
        return None

    inner_radii = [
        radius
        for group in [*top_circles, *end_circles]
        for radius in [min(group["radii"])]
    ]
    drawing_hole_diameter = 2 * float(median(inner_radii))
    if any(
        abs(2 * radius - drawing_hole_diameter) / drawing_hole_diameter > 0.08
        for radius in inner_radii
    ):
        return None

    centers: list[tuple[str, float, float]] = []
    top_middle_y = (top_region["min_y"] + top_region["max_y"]) / 2
    for group in top_circles:
        center_x, center_y = group["center"]
        centers.append((
            "Top",
            (center_x - main_region["min_x"]) * scale,
            -(center_y - top_middle_y) * scale - thickness_mm / 2,
        ))

    end_middle_x = (end_region["min_x"] + end_region["max_x"]) / 2
    for group in end_circles:
        center_x, center_y = group["center"]
        centers.append((
            "Right",
            -(center_x - end_middle_x) * scale - thickness_mm / 2,
            (center_y - profile_min_y) * scale,
        ))

    countersink_diameters = [
        2 * max(group["radii"]) * scale
        for group in end_circles
        if max(group["radii"]) > min(group["radii"]) * 1.25
    ]
    countersink_diameter = (
        float(median(countersink_diameters))
        if countersink_diameters
        else None
    )
    if countersink_diameter is None:
        for dimension in dimensions:
            text = dimension.get("text_override", "")
            if dimension.get("type") == "diameter" and "90" in text:
                countersink_diameter = float(dimension.get("value", 0)) or None
                break
    return centers, drawing_hole_diameter * scale, countersink_diameter


def build_preliminary_dxf_part(file_path: str | Path) -> PreliminaryModel:
    """Build an editable extruded profile when orthographic evidence is adequate.

    The model includes holes only if circular geometry is consistently observed
    in the matching plan and end projections. Countersink cuts are not created.
    """
    try:
        import ezdxf
    except ImportError as exc:
        raise PreliminaryModelError("Preliminary DXF modeling requires ezdxf.") from exc

    path = Path(file_path)
    report = analyze_dxf_drawing(path)
    if report["document_units"] is None:
        raise PreliminaryModelError(
            "DXF units are unspecified; a physical model scale cannot be inferred."
        )
    dimensions = report["dimensions"]
    regions = report["candidate_regions"]
    if not regions:
        raise PreliminaryModelError("No candidate orthographic geometry was found.")

    try:
        document = ezdxf.readfile(str(path))
        entities = {entity.dxf.handle: entity for entity in document.modelspace()}
    except (OSError, ValueError, ezdxf.DXFError) as exc:
        raise PreliminaryModelError(f"Could not read DXF geometry: {exc}") from exc

    alignments = {
        region["candidate_id"]: _alignment_ratio(region, entities)
        for region in regions
    }
    scored_regions = []
    for region in regions:
        match = _dimension_match(region, dimensions)
        alignment = alignments[region["candidate_id"]]
        if match is None or alignment < 0.35:
            continue
        mismatch, scale = match
        score = (
            float(region["width"]) * float(region["height"]) * alignment
            / (1 + mismatch * 8)
        )
        scored_regions.append((score, mismatch, scale, region))
    if not scored_regions:
        raise PreliminaryModelError(
            "No mostly orthogonal candidate view matched at least two native "
            "linear dimensions at a consistent drawing scale."
        )

    _, _, scale, main_region = max(scored_regions, key=lambda result: result[0])
    cycles = _profile_cycles(main_region, entities)
    profile = next((
        cycle for cycle in cycles
        if cycle[0] >= main_region["width"] * main_region["height"] * 0.01
        and max(point[0] for point in cycle[1]) - min(point[0] for point in cycle[1])
        >= main_region["width"] * 0.8
        and max(point[1] for point in cycle[1]) - min(point[1] for point in cycle[1])
        >= main_region["height"] * 0.8
    ), None)
    if profile is None:
        raise PreliminaryModelError(
            f"{main_region['candidate_id']} contains no sufficiently complete "
            "closed profile; no solid was created."
        )

    thickness = _infer_thickness(
        regions, alignments, main_region, scale, dimensions
    )
    _, profile_points, profile_handles = profile
    profile_points = _simplify_closed_polygon(profile_points)
    min_x = min(point[0] for point in profile_points)
    min_y = min(point[1] for point in profile_points)
    max_x = max(point[0] for point in profile_points)
    max_y = max(point[1] for point in profile_points)
    scaled_points = [
        ((x - min_x) * scale, (y - min_y) * scale)
        for x, y in profile_points
    ]

    sketch = Sketch(
        name="PreliminaryProfile",
        plane="Front",
        entities=[Polygon(points=scaled_points)],
    )
    extrude = Extrude(
        name="PreliminaryExtrude",
        sketch=sketch.name,
        operation="boss",
        depth=thickness,
    )
    features = [sketch, extrude]
    hole_data = _detect_orthographic_holes(
        regions,
        entities,
        main_region,
        scale,
        thickness,
        min_y,
        dimensions,
    )
    hole_count = 0
    hole_diameter = None
    countersink_diameter = None
    if hole_data is not None:
        hole_positions, hole_diameter, countersink_diameter = hole_data
        for plane in ("Top", "Right"):
            positions = [
                (x, y) for hole_plane, x, y in hole_positions
                if hole_plane == plane
            ]
            if not positions:
                continue
            hole_sketch = Sketch(
                name=f"Detected{plane}Holes",
                plane=plane,
                entities=[
                    Circle(cx=x, cy=y, diameter=hole_diameter)
                    for x, y in positions
                ],
            )
            features.extend((
                hole_sketch,
                Extrude(
                    name=f"Cut{plane}Holes",
                    sketch=hole_sketch.name,
                    operation="cut",
                    end="through_all",
                    depth=thickness,
                ),
            ))
            hole_count += len(positions)

    part = Part(name=f"{path.stem} Preliminary", features=features)
    if hole_count:
        omitted_note = (
            f"Added {hole_count} through-hole bores (estimated Ø"
            f"{hole_diameter:.2f} mm) from repeated orthographic circles. "
            f"The detected countersink opening"
            + (
                f" (about Ø{countersink_diameter:.2f} mm, 90°)"
                if countersink_diameter is not None
                else ""
            )
            + " is not modeled; confirm the bore size and hole positions."
        )
    else:
        omitted_note = (
            "Hole callouts or circles were present, but matching plan and end "
            "projections were not both confirmed; no holes were added."
        )
    return PreliminaryModel(
        part=part,
        source_region=main_region["candidate_id"],
        drawing_scale=scale,
        profile_width_mm=(max_x - min_x) * scale,
        profile_height_mm=(max_y - min_y) * scale,
        thickness_mm=thickness,
        profile_entity_handles=sorted(set(profile_handles)),
        hole_count=hole_count,
        hole_diameter_mm=hole_diameter,
        countersink_diameter_mm=countersink_diameter,
        review_note=omitted_note,
    )
