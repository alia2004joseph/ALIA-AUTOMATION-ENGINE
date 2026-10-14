"""
Generic, part-independent checks run before anything is previewed or built.

Errors block the build; warnings are shown but allowed.
"""
import base64
import binascii
import logging
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .model import (
    END_CONDITIONS, DIRECTIONS, OPERATIONS, PLANES,
    Circle, EdgeModifier, Extrude, Part, Polygon, Rect, Revolve, Sketch,
    Sketch3D, WeldmentMember,
)

logger = logging.getLogger(__name__)


@dataclass
class Issue:
    level: str      # "error" | "warning"
    feature: str    # feature name ("" for part-level)
    message: str

    def __str__(self) -> str:
        where = f"[{self.feature}] " if self.feature else ""
        return f"{'ERROR' if self.level == 'error' else 'warn '} {where}{self.message}"


def _closed_path_shapes(segments):
    """Convert closed, connected line paths to polygon or rectangle records."""
    tolerance = 1e-5
    edges = []
    adjacency = {}

    def point_key(point):
        return tuple(round(value / tolerance) for value in point)

    point_values = {}
    for start, end in segments:
        if math.dist(start, end) <= tolerance:
            continue
        start_key, end_key = point_key(start), point_key(end)
        if start_key == end_key:
            continue
        edge_index = len(edges)
        edges.append((start_key, end_key))
        point_values.setdefault(start_key, start)
        point_values.setdefault(end_key, end)
        adjacency.setdefault(start_key, []).append(edge_index)
        adjacency.setdefault(end_key, []).append(edge_index)

    shapes = []
    unseen = set(range(len(edges)))
    while unseen:
        first_edge = unseen.pop()
        component_edges = {first_edge}
        component_points = set(edges[first_edge])
        pending = list(component_points)
        while pending:
            point = pending.pop()
            for edge_index in adjacency[point]:
                if edge_index not in component_edges:
                    component_edges.add(edge_index)
                    for endpoint in edges[edge_index]:
                        if endpoint not in component_points:
                            component_points.add(endpoint)
                            pending.append(endpoint)
        unseen.difference_update(component_edges)
        if len(component_edges) < 3 or any(
            len([edge for edge in adjacency[point] if edge in component_edges]) != 2
            for point in component_points
        ):
            continue

        start = min(component_points)
        path_keys = [start]
        previous_edge = None
        current = start
        while True:
            candidates = [
                edge_index for edge_index in adjacency[current]
                if edge_index in component_edges and edge_index != previous_edge
            ]
            if not candidates:
                break
            edge_index = min(candidates)
            edge = edges[edge_index]
            following = edge[1] if edge[0] == current else edge[0]
            if following == start:
                break
            if following in path_keys:
                path_keys = []
                break
            path_keys.append(following)
            current, previous_edge = following, edge_index
        if len(path_keys) < 3:
            continue

        points = [point_values[key] for key in path_keys]
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        width, height = max_x - min_x, max_y - min_y
        if width <= tolerance or height <= tolerance:
            continue

        is_rectangle = (
            len(points) == 4
            and len({round(x / tolerance) for x in xs}) == 2
            and len({round(y / tolerance) for y in ys}) == 2
            and all(
                abs(points[i][0] - points[(i + 1) % 4][0]) <= tolerance
                or abs(points[i][1] - points[(i + 1) % 4][1]) <= tolerance
                for i in range(4)
            )
        )
        if is_rectangle:
            shapes.append({
                "type": "rect",
                "cx": (min_x + max_x) / 2,
                "cy": (min_y + max_y) / 2,
                "width": width,
                "height": height,
            })
        else:
            shapes.append({
                "type": "polygon",
                "points": [(point[0], point[1]) for point in points],
            })
    return shapes


def parse_blueprint_vectors(file_path: str, log=None) -> list:
    """Extract closed vector paths from PDF or DXF files into sketch entities.

    PDF coordinates are converted from points to mm. DXF coordinates are
    converted from the document's declared insertion units to mm. PDFs with
    embedded raster pages and no usable vector outlines are rendered at 300
    DPI and passed through the optional OpenCV raster contour parser. Native
    PDF entities are first normalized and classified deterministically so
    annotation-like content can be separated from profile geometry.
    """
    path = Path(file_path)
    extension = path.suffix.lower()
    if extension not in (".pdf", ".dxf"):
        return []

    def report(message):
        if log is not None:
            log(message)

    try:
        if extension == ".pdf":
            shapes = []
            raster_pages = []
            pages_requiring_review = []
            try:
                from ..drawing.pdf_drawing import (
                    ROLE_GEOMETRY,
                    ROLE_UNKNOWN,
                    parse_pdf_drawing,
                )

                drawing = parse_pdf_drawing(path)
                scale = 25.4 / 72.0
                for page in drawing.pages:
                    has_raster_content = page.raster_image_bbox_pt is not None
                    if (
                        has_raster_content
                        and page.native_word_count
                        and not page.has_native_strokes
                    ):
                        pages_requiring_review.append(page.page_number)
                        continue
                    for entity in page.entities:
                        if entity.kind == "rectangle" and entity.role in {ROLE_GEOMETRY, ROLE_UNKNOWN}:
                            bbox = entity.bbox_pt
                            if not bbox:
                                continue
                            x0 = float(bbox["x0"]) * scale
                            x1 = float(bbox["x1"]) * scale
                            y0 = float(bbox["top"]) * scale
                            y1 = float(bbox["bottom"]) * scale
                            if x1 > x0 and y1 > y0:
                                shapes.append({
                                    "type": "rect",
                                    "cx": (x0 + x1) / 2,
                                    "cy": (y0 + y1) / 2,
                                    "width": x1 - x0,
                                    "height": y1 - y0,
                                })
                    segments = []
                    for entity in page.entities:
                        if entity.kind != "line" or entity.role not in {ROLE_GEOMETRY, ROLE_UNKNOWN}:
                            continue
                        endpoints = entity.geometry.get("endpoints_pt", [])
                        if len(endpoints) != 2:
                            continue
                        segments.append((
                            (float(endpoints[0][0]) * scale, float(endpoints[0][1]) * scale),
                            (float(endpoints[1][0]) * scale, float(endpoints[1][1]) * scale),
                        ))
                    shapes.extend(_closed_path_shapes(segments))
                    if has_raster_content and (not page.has_native_strokes or not shapes):
                        if page.raster_image_bbox_pt is not None:
                            raster_pages.append((page.page_number - 1, page.raster_image_bbox_pt))
            except (OSError, KeyError, TypeError, ValueError) as exc:
                logger.warning("Could not read vector PDF %s: %s", path, exc)
                return []
            if pages_requiring_review:
                message = (
                    "[INGESTION] PDF page(s) "
                    f"{', '.join(map(str, pages_requiring_review))} contain native text "
                    "and raster artwork but no usable native drawing strokes. "
                    "Automatic contour tracing is unsafe for a sheet with dimensions, "
                    "annotations, or multiple views; no geometry was imported. Use a "
                    "single-view profile or enter/review the geometry manually."
                )
                logger.warning("%s: %s", path.name, message)
                report(message)
                return []
            shapes = _deduplicate_shapes(shapes)
            if shapes and not raster_pages:
                return shapes

            if not raster_pages:
                if shapes:
                    return shapes
                logger.info("PDF contains no closed vector paths or raster pages: %s", path)
                return []

            report(
                "[ALIA ENGINE]: Scanned drawing image sheet detected. "
                "Running pixel edge trace optimization pipeline..."
            )
            try:
                import cv2
                import pypdfium2 as pdfium
                from .raster_parser import convert_raster_array_to_vectors

                logger.debug("Raster PDF fallback using OpenCV %s", cv2.__version__)
                raster_shapes = []
                document = pdfium.PdfDocument(str(path))
                try:
                    for page_index, image_bbox in raster_pages:
                        page = document[page_index]
                        bitmap = page.render(scale=300 / 72)
                        page_image = bitmap.to_pil()
                        left, top, right, bottom = (
                            round(coordinate * 300 / 72) for coordinate in image_bbox
                        )
                        cropped = page_image.crop((left, top, right, bottom))
                        raster_shapes.extend(convert_raster_array_to_vectors(cropped, target_dpi=300))
                        cropped.close()
                        page_image.close()
                        bitmap.close()
                        page.close()
                finally:
                    document.close()
            except Exception as exc:
                logger.warning("Could not vectorize raster PDF %s: %s", path, exc)
                report(f"[INGESTION] Raster vectorization unavailable: {exc}")
                return shapes
            if raster_shapes:
                report(f"[INGESTION] Raster contour tracing extracted {len(raster_shapes)} closed paths.")
                return raster_shapes
            else:
                report("[INGESTION] Raster tracing found no usable closed contours.")
            return shapes

        import ezdxf
        from ezdxf import units

        try:
            document = ezdxf.readfile(str(path))
            scale = (
                units.conversion_factor(document.units, units.MM)
                if document.units
                else 1.0
            )
            modelspace = document.modelspace()
            segments = []
            shapes = []
            for entity in modelspace:
                kind = entity.dxftype()
                if kind == "LINE":
                    start, end = entity.dxf.start, entity.dxf.end
                    segments.append(((start.x * scale, start.y * scale),
                                     (end.x * scale, end.y * scale)))
                elif kind == "LWPOLYLINE" and entity.closed:
                    points = [(point[0] * scale, point[1] * scale) for point in entity.get_points()]
                    shapes.extend(_shape_from_closed_points(points))
                elif kind == "POLYLINE" and entity.is_closed:
                    points = [(vertex.dxf.location.x * scale, vertex.dxf.location.y * scale)
                              for vertex in entity.vertices]
                    shapes.extend(_shape_from_closed_points(points))
        except (ezdxf.DXFError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            logger.warning("Could not read vector DXF %s: %s", path, exc)
            return []
        shapes.extend(_closed_path_shapes(segments))
        return _deduplicate_shapes(shapes)
    except Exception:
        logger.exception("Vector drawing ingestion failed for %s", path)
        return []


def _shape_from_closed_points(points):
    if len(points) < 3:
        return []
    if math.dist(points[0], points[-1]) <= 1e-5:
        points = points[:-1]
    if len(points) < 3:
        return []
    min_x, max_x = min(p[0] for p in points), max(p[0] for p in points)
    min_y, max_y = min(p[1] for p in points), max(p[1] for p in points)
    width, height = max_x - min_x, max_y - min_y
    if width <= 0 or height <= 0:
        return []
    is_rectangle = (
        len(points) == 4
        and len({round(p[0], 6) for p in points}) == 2
        and len({round(p[1], 6) for p in points}) == 2
        and all(
            abs(points[i][0] - points[(i + 1) % 4][0]) <= 1e-5
            or abs(points[i][1] - points[(i + 1) % 4][1]) <= 1e-5
            for i in range(4)
        )
    )
    if is_rectangle:
        return [{
            "type": "rect", "cx": (min_x + max_x) / 2,
            "cy": (min_y + max_y) / 2, "width": width, "height": height,
        }]
    return [{"type": "polygon", "points": points}]


def _deduplicate_shapes(shapes):
    unique = []
    seen = set()
    for shape in shapes:
        if shape["type"] == "rect":
            key = (
                shape["type"], round(shape["cx"], 5), round(shape["cy"], 5),
                round(shape["width"], 5), round(shape["height"], 5),
            )
        elif shape["type"] == "circle":
            key = (
                shape["type"], round(shape["cx"], 5), round(shape["cy"], 5),
                round(shape["diameter"], 5),
            )
        else:
            key = (shape["type"], tuple((round(x, 5), round(y, 5)) for x, y in shape["points"]))
        if key not in seen:
            seen.add(key)
            unique.append(shape)
    return unique


def _round_dxf_mm(value: float) -> float:
    return round(float(value) * 2.0) / 2.0


def _dxf_layer_role(layer: str) -> Optional[str]:
    tokens = set(re.findall(r"[A-Z0-9]+", layer.upper()))
    if tokens & {"CUT", "HOLE", "HOLES", "VOID", "POCKET"}:
        return "cut"
    if tokens & {"BOSS", "OUTER", "PROFILE", "SOLID"}:
        return "boss"
    return None


def _dxf_is_annotation_layer(layer: str) -> bool:
    tokens = set(re.findall(r"[A-Z0-9]+", layer.upper()))
    return bool(tokens & {
        "DIM", "DIMS", "DIMENSION", "DIMENSIONS", "ANNOTATION", "TEXT",
        "CENTER", "HIDDEN", "DEFPOINTS",
    })


def _dxf_profile_area(shape: dict) -> float:
    if shape["type"] == "circle":
        return math.pi * (shape["diameter"] / 2.0) ** 2
    if shape["type"] == "rect":
        return shape["width"] * shape["height"]
    return abs(polygon_area(shape["points"]))


def _dxf_profile_probe_points(shape: dict) -> list:
    if shape["type"] == "circle":
        radius = shape["diameter"] / 2.0
        return [
            (shape["cx"], shape["cy"]),
            (shape["cx"] + radius, shape["cy"]),
            (shape["cx"] - radius, shape["cy"]),
            (shape["cx"], shape["cy"] + radius),
            (shape["cx"], shape["cy"] - radius),
        ]
    if shape["type"] == "rect":
        half_width, half_height = shape["width"] / 2.0, shape["height"] / 2.0
        return [
            (shape["cx"] - half_width, shape["cy"] - half_height),
            (shape["cx"] + half_width, shape["cy"] - half_height),
            (shape["cx"] + half_width, shape["cy"] + half_height),
            (shape["cx"] - half_width, shape["cy"] + half_height),
        ]
    return shape["points"]


def _dxf_point_in_polygon(point, polygon) -> bool:
    x, y = point
    inside = False
    for index, (x1, y1) in enumerate(polygon):
        x2, y2 = polygon[(index + 1) % len(polygon)]
        cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
        if abs(cross) <= 1e-8 and (
            min(x1, x2) - 1e-8 <= x <= max(x1, x2) + 1e-8
            and min(y1, y2) - 1e-8 <= y <= max(y1, y2) + 1e-8
        ):
            return False
        if (y1 > y) != (y2 > y):
            crossing_x = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < crossing_x:
                inside = not inside
    return inside


def _dxf_contains(outer: dict, inner: dict) -> bool:
    if outer["type"] == "circle":
        outer_radius = outer["diameter"] / 2.0
        return all(
            math.hypot(x - outer["cx"], y - outer["cy"]) < outer_radius - 1e-8
            for x, y in _dxf_profile_probe_points(inner)
        )
    if outer["type"] == "rect":
        min_x = outer["cx"] - outer["width"] / 2.0
        max_x = outer["cx"] + outer["width"] / 2.0
        min_y = outer["cy"] - outer["height"] / 2.0
        max_y = outer["cy"] + outer["height"] / 2.0
        return all(
            min_x + 1e-8 < x < max_x - 1e-8
            and min_y + 1e-8 < y < max_y - 1e-8
            for x, y in _dxf_profile_probe_points(inner)
        )
    return all(
        _dxf_point_in_polygon(point, outer["points"])
        for point in _dxf_profile_probe_points(inner)
    )


def _dxf_arc_points(cx: float, cy: float, radius: float, start: float, end: float) -> list:
    sweep = end - start
    while sweep <= 0:
        sweep += 360.0
    if radius <= 0:
        return []
    max_angle = 2.0 * math.acos(max(-1.0, min(1.0, 1.0 - min(0.15 / radius, 1.0))))
    step_angle = max(math.degrees(max_angle), 1.0)
    count = max(2, math.ceil(sweep / step_angle))
    return [
        (
            cx + radius * math.cos(math.radians(start + sweep * index / count)),
            cy + radius * math.sin(math.radians(start + sweep * index / count)),
        )
        for index in range(count + 1)
    ]


def _dxf_bulge_points(start, end, bulge: float) -> list:
    if abs(bulge) <= 1e-12:
        return [start, end]
    dx, dy = end[0] - start[0], end[1] - start[1]
    chord = math.hypot(dx, dy)
    if chord <= 1e-12:
        return [start, end]
    sweep = 4.0 * math.atan(bulge)
    offset = chord / (2.0 * math.tan(sweep / 2.0))
    center_x = (start[0] + end[0]) / 2.0 - dy / chord * offset
    center_y = (start[1] + end[1]) / 2.0 + dx / chord * offset
    radius = math.hypot(start[0] - center_x, start[1] - center_y)
    count = max(2, math.ceil(abs(math.degrees(sweep)) / 5.0))
    start_angle = math.atan2(start[1] - center_y, start[0] - center_x)
    return [
        (
            center_x + radius * math.cos(start_angle + sweep * index / count),
            center_y + radius * math.sin(start_angle + sweep * index / count),
        )
        for index in range(count)
    ] + [end]


def parse_universal_dxf(file_path: str) -> dict:
    """Parse planar DXF profiles into explicit boss, cut, and modifier groups.

    Supported profile sources are LINE loops, planar ARC chains, closed
    LWPOLYLINE/POLYLINE entities, and CIRCLE entities. Text, dimensions,
    construction geometry, and unsupported entities are ignored. Unlabelled
    closed loops use even/odd containment; explicit layer tokens BOSS/OUTER/
    PROFILE/SOLID and CUT/HOLE/VOID/POCKET override that inference.

    DXF geometry does not contain extrusion depths or dependable 3D edge
    references. Those must be supplied by the build recipe; only entities on
    explicitly named FILLET or CHAMFER layers are reported as modifier hints.
    """
    path = Path(file_path)
    if not path.is_file():
        return {"status": "error", "note": f"Target DXF file path missing: '{path}'."}

    try:
        import ezdxf
    except ImportError as exc:
        return {"status": "error", "note": f"DXF parsing requires ezdxf: {exc}"}
    try:
        from ezdxf import units
        document = ezdxf.readfile(str(path))
    except (OSError, ValueError, ezdxf.DXFError) as exc:
        logger.warning("Could not read universal DXF %s: %s", path, exc)
        return {"status": "error", "note": f"Invalid DXF structure: {exc}"}

    try:
        if not document.units:
            return {
                "status": "error",
                "note": "DXF units are unspecified; set the document INSUNITS value before compiling.",
            }
        scale = units.conversion_factor(document.units, units.MM)
        modelspace = document.modelspace()
    except (ValueError, TypeError, AttributeError) as exc:
        return {"status": "error", "note": f"DXF units could not be converted to millimetres: {exc}"}

    profiles = []
    modifier_hints = []
    segments_by_group = {}
    unsupported_count = 0

    def group_key(layer: str, elevation: float):
        return layer, round(elevation, 6)

    def add_polyline(layer: str, elevation: float, points, bulges, closed: bool):
        if len(points) < 2:
            return
        expanded = []
        edge_count = len(points) if closed else len(points) - 1
        for index in range(edge_count):
            start = points[index]
            end = points[(index + 1) % len(points)]
            arc_points = _dxf_bulge_points(start, end, bulges[index])
            expanded.extend(arc_points[:-1])
        if not closed:
            expanded.append(points[-1])
        if closed:
            shape_records = _shape_from_closed_points(expanded)
            for shape in shape_records:
                profiles.append((layer, elevation, shape))
        else:
            key = group_key(layer, elevation)
            segments = segments_by_group.setdefault(key, [])
            segments.extend(zip(expanded, expanded[1:]))

    try:
        for entity in modelspace:
            kind = entity.dxftype()
            layer = str(entity.dxf.layer)
            layer_tokens = set(re.findall(r"[A-Z0-9]+", layer.upper()))
            if _dxf_is_annotation_layer(layer):
                unsupported_count += 1
                continue
            modifier_type = (
                "fillet" if "FILLET" in layer_tokens
                else "chamfer" if "CHAMFER" in layer_tokens
                else None
            )

            if kind == "LINE":
                start, end = entity.dxf.start, entity.dxf.end
                if abs(start.z - end.z) > 1e-6:
                    unsupported_count += 1
                    continue
                p1 = (start.x * scale, start.y * scale)
                p2 = (end.x * scale, end.y * scale)
                elevation = start.z * scale
                if modifier_type:
                    modifier_hints.append({
                        "type": modifier_type,
                        "layer": layer,
                        "geometry": {
                            "type": "line",
                            "x0": _round_dxf_mm(p1[0]),
                            "y0": _round_dxf_mm(p1[1]),
                            "x1": _round_dxf_mm(p2[0]),
                            "y1": _round_dxf_mm(p2[1]),
                        },
                    })
                else:
                    segments_by_group.setdefault(group_key(layer, elevation), []).append((p1, p2))
            elif kind in ("LWPOLYLINE", "POLYLINE"):
                if kind == "LWPOLYLINE":
                    vertices = list(entity.get_points("xyb"))
                    elevation = float(entity.dxf.elevation) * scale
                    points = [(float(v[0]) * scale, float(v[1]) * scale) for v in vertices]
                    bulges = [float(v[2]) for v in vertices]
                    closed = bool(entity.closed)
                else:
                    vertices = list(entity.vertices)
                    locations = [vertex.dxf.location for vertex in vertices]
                    elevations = [location.z * scale for location in locations]
                    if elevations and max(elevations) - min(elevations) > 1e-6:
                        unsupported_count += 1
                        continue
                    elevation = elevations[0] if elevations else 0.0
                    points = [(location.x * scale, location.y * scale) for location in locations]
                    bulges = [float(vertex.dxf.get("bulge", 0.0)) for vertex in vertices]
                    closed = bool(entity.is_closed)
                if modifier_type:
                    if closed:
                        unsupported_count += 1
                    else:
                        for start, end in zip(points, points[1:]):
                            modifier_hints.append({
                                "type": modifier_type,
                                "layer": layer,
                                "geometry": {
                                    "type": "line",
                                    "x0": _round_dxf_mm(start[0]),
                                    "y0": _round_dxf_mm(start[1]),
                                    "x1": _round_dxf_mm(end[0]),
                                    "y1": _round_dxf_mm(end[1]),
                                },
                            })
                else:
                    add_polyline(layer, elevation, points, bulges, closed)
            elif kind == "CIRCLE":
                center = entity.dxf.center
                radius = float(entity.dxf.radius) * scale
                if modifier_type:
                    modifier_hints.append({
                        "type": modifier_type,
                        "layer": layer,
                        "geometry": {
                            "type": "arc",
                            "cx": _round_dxf_mm(center.x * scale),
                            "cy": _round_dxf_mm(center.y * scale),
                            "radius_mm": _round_dxf_mm(radius),
                            "elevation_mm": _round_dxf_mm(center.z * scale),
                        },
                    })
                elif radius > 0:
                    profiles.append((layer, center.z * scale, {
                        "type": "circle",
                        "cx": center.x * scale,
                        "cy": center.y * scale,
                        "diameter": radius * 2.0,
                    }))
            elif kind == "ARC":
                center = entity.dxf.center
                radius = float(entity.dxf.radius) * scale
                points = _dxf_arc_points(
                    center.x * scale,
                    center.y * scale,
                    radius,
                    float(entity.dxf.start_angle),
                    float(entity.dxf.end_angle),
                )
                if modifier_type:
                    modifier_hints.append({
                        "type": modifier_type,
                        "layer": layer,
                        "geometry": {
                            "type": "arc",
                            "cx": _round_dxf_mm(center.x * scale),
                            "cy": _round_dxf_mm(center.y * scale),
                            "radius_mm": _round_dxf_mm(radius),
                            "elevation_mm": _round_dxf_mm(center.z * scale),
                            "start_angle": float(entity.dxf.start_angle),
                            "end_angle": float(entity.dxf.end_angle),
                        },
                    })
                else:
                    elevation = center.z * scale
                    segments = segments_by_group.setdefault(group_key(layer, elevation), [])
                    segments.extend(zip(points, points[1:]))
            else:
                unsupported_count += 1
    except (ezdxf.DXFError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        logger.warning("Could not interpret DXF geometry in %s: %s", path, exc)
        return {"status": "error", "note": f"Could not interpret DXF geometry: {exc}"}

    for (layer, elevation), segments in segments_by_group.items():
        for shape in _closed_path_shapes(segments):
            profiles.append((layer, elevation, shape))

    if not profiles and not modifier_hints:
        return {
            "status": "mismatch",
            "note": "No supported closed planar profiles or explicitly tagged modifiers were found.",
            "ignored_entities": unsupported_count,
        }

    profile_data = []
    for layer, elevation, shape in profiles:
        rounded = dict(shape)
        if shape["type"] == "rect":
            rounded.update({
                key: _round_dxf_mm(shape[key])
                for key in ("cx", "cy", "width", "height")
            })
        elif shape["type"] == "circle":
            rounded.update({
                key: _round_dxf_mm(shape[key])
                for key in ("cx", "cy", "diameter")
            })
        else:
            rounded["points"] = [
                (_round_dxf_mm(x), _round_dxf_mm(y))
                for x, y in shape["points"]
            ]
        if (
            rounded["type"] == "polygon" and len(set(rounded["points"])) < 3
            or rounded["type"] == "circle" and rounded["diameter"] <= 0
            or rounded["type"] == "rect"
            and (rounded["width"] <= 0 or rounded["height"] <= 0)
        ):
            continue
        profile_data.append({
            "layer": layer,
            "elevation_mm": _round_dxf_mm(elevation),
            "entities": [rounded],
            "area_mm2": _dxf_profile_area(rounded),
            "layer_role": _dxf_layer_role(layer),
        })

    boss_geometry, cut_geometry = [], []
    for item in profile_data:
        parents = [
            candidate for candidate in profile_data
            if candidate is not item
            and candidate["area_mm2"] > item["area_mm2"]
            and candidate["elevation_mm"] == item["elevation_mm"]
            and _dxf_contains(candidate["entities"][0], item["entities"][0])
        ]
        nesting_depth = len(parents)
        role = item["layer_role"] or ("cut" if nesting_depth % 2 else "boss")
        target = cut_geometry if role == "cut" else boss_geometry
        target.append({
            "id": f"profile-{len(boss_geometry) + len(cut_geometry) + 1:04d}",
            "layer": item["layer"],
            "elevation_mm": item["elevation_mm"],
            "nesting_depth": nesting_depth,
            "entities": item["entities"],
        })

    for index, modifier in enumerate(modifier_hints, 1):
        modifier["id"] = f"modifier-{index:04d}"
        if "geometry" in modifier:
            geometry = modifier["geometry"]
            for key in (
                "cx", "cy", "radius_mm", "elevation_mm",
                "x0", "y0", "x1", "y1",
            ):
                if key in geometry:
                    geometry[key] = _round_dxf_mm(geometry[key])

    if not boss_geometry:
        return {
            "status": "mismatch",
            "note": "DXF profiles contain no base boss profile.",
            "boss_geometry": [],
            "cut_geometry": cut_geometry,
            "modifiers": modifier_hints,
        }
    if unsupported_count:
        logger.info("Ignored %d unsupported or non-planar DXF entities in %s.", unsupported_count, path)
    return {
        "status": "ok",
        "part_name": path.stem,
        "units": "mm",
        "boss_geometry": boss_geometry,
        "cut_geometry": cut_geometry,
        "modifiers": modifier_hints,
        "ignored_entities": unsupported_count,
    }


def segment_and_flatten_blueprint(image_path: str) -> dict:
    """Diagnose a raster engineering sheet and extract orthogonal front-view lines.

    The configured ROI follows the expected page layout (front view in the
    lower-left). Coordinates are relative to that ROI's lower-left corner.
    The conversion to millimeters assumes its longest horizontal profile line
    represents a 60 mm baseline.
    """
    path = Path(image_path)
    if not path.is_file():
        return {"status": "error", "note": f"Target image file missing: '{path}'."}

    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        return {
            "status": "error",
            "note": f"OpenCV and NumPy are required for input diagnosis: {exc}",
        }

    try:
        if path.suffix.lower() == ".pdf":
            import pdfplumber
            import pypdfium2 as pdfium

            with pdfplumber.open(path) as document:
                if not document.pages:
                    return {"status": "error", "note": "The PDF contains no pages."}
                page_index = 0
                page = document.pages[page_index]
                image = max(
                    page.images,
                    key=lambda item: item["width"] * item["height"],
                    default=None,
                )
                if image is None:
                    return {
                        "status": "mismatch",
                        "note": "No embedded raster image found for raster diagnosis.",
                    }
                bbox = (image["x0"], image["top"], image["x1"], image["bottom"])
            pdf = pdfium.PdfDocument(str(path))
            try:
                pdf_page = pdf[page_index]
                bitmap = pdf_page.render(scale=300 / 72)
                rendered = bitmap.to_pil()
                crop_box = tuple(round(value * 300 / 72) for value in bbox)
                raster = np.asarray(rendered.crop(crop_box).convert("L"))
                rendered.close()
                bitmap.close()
                pdf_page.close()
            finally:
                pdf.close()
        else:
            raster = cv2.imread(os.fspath(path), cv2.IMREAD_GRAYSCALE)
            if raster is None:
                return {
                    "status": "error",
                    "note": "OpenCV could not decode the selected image file.",
                }
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        logger.warning("Could not load raster drawing for diagnosis %s: %s", path, exc)
        return {"status": "error", "note": f"Could not load drawing image: {exc}"}

    if raster.ndim != 2 or raster.size == 0:
        return {"status": "error", "note": "Drawing image is empty or not grayscale."}
    height, width = raster.shape
    if height < 8 or width < 8:
        return {"status": "error", "note": "Drawing image is too small for view segmentation."}

    front_roi = raster[int(height * 0.45):int(height * 0.95), 0:int(width * 0.55)]
    if front_roi.size == 0:
        return {"status": "error", "note": "Front-view ROI is empty."}

    blurred = cv2.GaussianBlur(front_roi, (5, 5), 0)
    edges = cv2.Canny(blurred, 50, 150, apertureSize=3)
    detected_lines = cv2.HoughLinesP(
        edges,
        1,
        np.pi / 180,
        threshold=80,
        minLineLength=40,
        maxLineGap=10,
    )
    if detected_lines is None:
        return {
            "status": "mismatch",
            "note": "No structural paths tracked in the front-view ROI. Verify view placement and print resolution.",
        }

    lines = detected_lines.reshape(-1, 4)
    horizontal_widths = [
        abs(int(x2) - int(x1))
        for x1, y1, x2, y2 in lines
        if abs(int(y2) - int(y1)) < 5
    ]
    if not horizontal_widths:
        return {
            "status": "mismatch",
            "note": "No horizontal baseline found; cannot calibrate the 60 mm drawing width.",
        }
    baseline_pixels = max(horizontal_widths)
    if baseline_pixels <= 0:
        return {"status": "mismatch", "note": "Detected horizontal baseline has zero length."}

    pixel_to_mm_scale = 60.0 / baseline_pixels
    roi_height = front_roi.shape[0]
    raw_features = []
    for x1, y1, x2, y2 in lines:
        mx1, my1 = float(x1) * pixel_to_mm_scale, (roi_height - float(y1)) * pixel_to_mm_scale
        mx2, my2 = float(x2) * pixel_to_mm_scale, (roi_height - float(y2)) * pixel_to_mm_scale
        dx, dy = abs(mx1 - mx2), abs(my1 - my2)

        if dx > 0 and dy / dx < 0.08:
            mean_y = round((my1 + my2) / 2.0)
            mx1, mx2 = round(mx1), round(mx2)
            my1 = my2 = mean_y
        elif dy > 0 and dx / dy < 0.08:
            mean_x = round((mx1 + mx2) / 2.0)
            my1, my2 = round(my1), round(my2)
            mx1 = mx2 = mean_x
        else:
            continue

        raw_features.append({
            "type": "line",
            "x0": mx1,
            "y0": my1,
            "x1": mx2,
            "y1": my2,
        })

    if not raw_features:
        return {
            "status": "mismatch",
            "note": "Detected paths did not contain horizontal or vertical profile segments.",
        }
    return {
        "status": "ok",
        "units": "mm",
        "detected_view": "front",
        "features": raw_features,
        "confidence": "high_orthogonal_snapped",
        "calibration": {
            "baseline_assumed_mm": 60.0,
            "baseline_pixels": baseline_pixels,
            "pixel_to_mm_scale": pixel_to_mm_scale,
        },
    }


# ---------------------------------------------------------------- geometry
def _segments_cross(p1, p2, p3, p4) -> bool:
    def orient(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    d1, d2 = orient(p3, p4, p1), orient(p3, p4, p2)
    d3, d4 = orient(p1, p2, p3), orient(p1, p2, p4)
    return d1 * d2 < 0 and d3 * d4 < 0


def polygon_self_intersects(points) -> bool:
    n = len(points)
    for i in range(n):
        a1, a2 = points[i], points[(i + 1) % n]
        for j in range(i + 1, n):
            if (j + 1) % n == i or (i + 1) % n == j:
                continue
            if _segments_cross(a1, a2, points[j], points[(j + 1) % n]):
                return True
    return False


def polygon_area(points) -> float:
    s = 0.0
    for i in range(len(points)):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % len(points)]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def _relation(a, b):
    """'disjoint' | 'nested' | 'overlap' for rect/circle pairs (None if unsupported)."""
    if isinstance(a, Circle) and isinstance(b, Circle):
        d = math.hypot(a.cx - b.cx, a.cy - b.cy)
        r1, r2 = a.diameter / 2, b.diameter / 2
        if d >= r1 + r2:
            return "disjoint"
        if d + min(r1, r2) <= max(r1, r2):
            return "nested"
        return "overlap"
    if isinstance(a, Rect) and isinstance(b, Rect):
        ax, ay, bx, by = a.width / 2, a.height / 2, b.width / 2, b.height / 2
        dx, dy = abs(a.cx - b.cx), abs(a.cy - b.cy)
        if dx >= ax + bx or dy >= ay + by:
            return "disjoint"
        if (dx + bx <= ax and dy + by <= ay) or (dx + ax <= bx and dy + ay <= by):
            return "nested"
        return "overlap"
    if isinstance(a, Rect) and isinstance(b, Circle):
        a, b = b, a
    if isinstance(a, Circle) and isinstance(b, Rect):
        r = a.diameter / 2
        hw, hh = b.width / 2, b.height / 2
        dx, dy = abs(a.cx - b.cx), abs(a.cy - b.cy)
        if math.hypot(max(dx - hw, 0), max(dy - hh, 0)) >= r:
            return "disjoint"
        if dx + r <= hw and dy + r <= hh:
            return "nested"
        if math.hypot(dx + hw, dy + hh) <= r:
            return "nested"
        return "overlap"
    return None


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def _point_segment_distance(point, start, end) -> float:
    direction = tuple(end[i] - start[i] for i in range(3))
    length2 = sum(value * value for value in direction)
    if length2 == 0:
        return math.dist(point, start)
    t = sum((point[i] - start[i]) * direction[i] for i in range(3)) / length2
    t = max(0.0, min(1.0, t))
    closest = tuple(start[i] + t * direction[i] for i in range(3))
    return math.dist(point, closest)


def _segments_intersect_3d(first, second, tolerance=1e-6) -> bool:
    """Return true when two 3D line segments touch within the mm tolerance."""
    p1, q1 = first
    p2, q2 = second
    u = tuple(q1[i] - p1[i] for i in range(3))
    v = tuple(q2[i] - p2[i] for i in range(3))
    w = tuple(p1[i] - p2[i] for i in range(3))
    a, b, c = sum(x*x for x in u), sum(u[i]*v[i] for i in range(3)), sum(x*x for x in v)
    d, e = sum(u[i]*w[i] for i in range(3)), sum(v[i]*w[i] for i in range(3))
    denom = a * c - b * b
    if a == 0 or c == 0:
        return min(_point_segment_distance(p1, p2, q2),
                   _point_segment_distance(p2, p1, q1)) <= tolerance
    s = 0.0 if abs(denom) < 1e-15 else max(0.0, min(1.0, (b * e - c * d) / denom))
    t = max(0.0, min(1.0, (b * s + e) / c))
    s = max(0.0, min(1.0, (b * t - d) / a))
    closest1 = tuple(p1[i] + s * u[i] for i in range(3))
    closest2 = tuple(p2[i] + t * v[i] for i in range(3))
    return math.dist(closest1, closest2) <= tolerance


def _network_is_open_and_intersecting(sketch: Sketch3D) -> bool:
    def valid_point(point):
        return (
            isinstance(point, (tuple, list))
            and len(point) == 3
            and all(_finite(value) for value in point)
        )

    segments = [
        segment for segment in (sketch.segments or [])
        if isinstance(segment, (tuple, list))
        and len(segment) == 2
        and all(valid_point(point) for point in segment)
    ]
    if not segments:
        return False
    degrees = {}
    for start, end in segments:
        for point in (start, end):
            key = tuple(round(value, 6) for value in point)
            degrees[key] = degrees.get(key, 0) + 1
    is_open = any(degree == 1 for degree in degrees.values())
    if not is_open:
        return False
    for i, first in enumerate(segments):
        for second in segments[i + 1:]:
            shared = {tuple(round(v, 6) for v in p) for p in first} & {
                tuple(round(v, 6) for v in p) for p in second
            }
            if not shared and _segments_intersect_3d(first, second):
                return True
    return False


def _sketch_bounds(sketch: Sketch):
    xs, ys = [], []
    for entity in sketch.entities:
        if isinstance(entity, Rect):
            xs.extend((entity.cx - entity.width / 2, entity.cx + entity.width / 2))
            ys.extend((entity.cy - entity.height / 2, entity.cy + entity.height / 2))
        elif isinstance(entity, Circle):
            radius = entity.diameter / 2
            xs.extend((entity.cx - radius, entity.cx + radius))
            ys.extend((entity.cy - radius, entity.cy + radius))
        elif isinstance(entity, Polygon):
            xs.extend(point[0] for point in entity.points)
            ys.extend(point[1] for point in entity.points)
    if not xs or not ys:
        return None
    return max(xs) - min(xs), max(ys) - min(ys)


def _extrude_minimum_dimension(extrude: Extrude, sketch: Sketch) -> Optional[float]:
    bounds = _sketch_bounds(sketch)
    if bounds is None or extrude.end != "blind" or not _finite(extrude.depth):
        return None
    return min(bounds[0], bounds[1], extrude.depth)


# ---------------------------------------------------------------- checks
def _check_sketch(sk: Sketch, issues: List[Issue]) -> None:
    def err(msg):
        issues.append(Issue("error", sk.name, msg))

    if sk.plane not in PLANES:
        err(f"Unknown plane '{sk.plane}'.")
    if not sk.entities:
        err("Sketch is empty.")
    for i, e in enumerate(sk.entities, 1):
        label = f"{e.kind} #{i}"
        if isinstance(e, Rect) and (e.width <= 0 or e.height <= 0):
            err(f"{label}: width and height must be > 0.")
        elif isinstance(e, Circle) and e.diameter <= 0:
            err(f"{label}: diameter must be > 0.")
        elif isinstance(e, Polygon):
            if len(e.points) < 3:
                err(f"{label}: needs at least 3 points.")
            elif polygon_self_intersects(e.points):
                err(f"{label}: edges cross each other.")
            elif abs(polygon_area(e.points)) < 1e-9:
                err(f"{label}: has zero area.")
    for i in range(len(sk.entities)):
        for j in range(i + 1, len(sk.entities)):
            if _relation(sk.entities[i], sk.entities[j]) == "overlap":
                err(f"{sk.entities[i].kind} #{i + 1} and {sk.entities[j].kind} #{j + 1} "
                    "partially overlap (contours must be separate or fully nested).")


def validate(part: Part) -> List[Issue]:
    issues: List[Issue] = []
    seen = set()
    sketches = {}
    sketches_3d = {}
    prior_features = {}
    has_body = False
    used_sketches = set()

    for f in part.features:
        if not f.name.strip():
            issues.append(Issue("error", "", "A feature has an empty name."))
        elif f.name in seen:
            issues.append(Issue("error", f.name, "Duplicate feature name."))
        seen.add(f.name)

        if isinstance(f, Sketch):
            sketches[f.name] = f
            _check_sketch(f, issues)
        elif isinstance(f, Sketch3D):
            sketches_3d[f.name] = f
            for index, segment in enumerate(f.segments or [], 1):
                if (
                    not isinstance(segment, (tuple, list))
                    or len(segment) != 2
                    or any(not isinstance(point, (tuple, list)) or len(point) != 3 for point in segment)
                ):
                    issues.append(Issue("error", f.name, f"3D line #{index} must have two 3D endpoints."))
                    continue
                if any(not _finite(value) for point in segment for value in point):
                    issues.append(Issue("error", f.name, f"3D line #{index} contains a non-finite coordinate."))
                elif math.dist(segment[0], segment[1]) <= 1e-9:
                    issues.append(Issue("error", f.name, f"3D line #{index} has zero length."))
            if _network_is_open_and_intersecting(f):
                issues.append(Issue(
                    "warning", f.name,
                    "Open, intersecting 3D wire network detected; this is a likely weldment path. "
                    "Add a WeldmentMember with a valid .sldlfp profile to create solid members.",
                ))
        elif isinstance(f, Extrude):
            def err(msg, f=f):
                issues.append(Issue("error", f.name, msg))

            if f.operation not in OPERATIONS:
                err(f"Unknown operation '{f.operation}'.")
            if f.end not in END_CONDITIONS:
                err(f"Unknown end condition '{f.end}'.")
            if f.direction not in DIRECTIONS:
                err(f"Unknown direction '{f.direction}'.")
            if f.sketch not in sketches:
                err("Needs a sketch that appears earlier in the tree.")
            else:
                used_sketches.add(f.sketch)
            if f.end == "blind" and f.depth <= 0:
                err("Depth must be > 0.")
            if f.operation == "boss" and f.end == "through_all":
                err("A boss cannot be 'through_all'.")
            if f.operation == "cut" and not has_body:
                err("A cut needs an existing body (add a boss first).")
            if f.operation == "boss":
                has_body = True
        elif isinstance(f, Revolve):
            def err(msg, f=f):
                issues.append(Issue("error", f.name, msg))

            if f.operation not in OPERATIONS:
                err(f"Unknown operation '{f.operation}'.")
            if not _finite(f.angle) or not 0 < f.angle <= 360:
                err("Revolve angle must be finite and in the range (0, 360] degrees.")
            if not isinstance(f.axis_name, str) or not f.axis_name.strip():
                err("Needs the name of an existing SolidWorks axis or sketch axis.")
            if f.sketch_name not in sketches:
                err("Needs a 2D sketch that appears earlier in the tree.")
            else:
                used_sketches.add(f.sketch_name)
            if f.operation == "cut" and not has_body:
                err("A revolve cut needs an existing body (add a boss first).")
            if f.operation == "boss":
                has_body = True
        elif isinstance(f, EdgeModifier):
            def err(msg, f=f):
                issues.append(Issue("error", f.name, msg))

            if f.modifier_type not in ("fillet", "chamfer"):
                err(f"Unknown edge modifier type '{f.modifier_type}'.")
            if not _finite(f.radius_mm) or f.radius_mm <= 0:
                err("Modifier size must be a finite value greater than 0 mm.")
            if not isinstance(f.target_edges, list) or not f.target_edges:
                err("At least one persistent edge reference is required.")
            for token in f.target_edges if isinstance(f.target_edges, list) else []:
                try:
                    if not isinstance(token, str) or not base64.b64decode(token, validate=True):
                        raise ValueError
                except (TypeError, ValueError, binascii.Error):
                    err("Edge references must be non-empty base64 SolidWorks persistent-reference tokens.")
                    break
            target = (
                prior_features.get(f.feature_target)
                if isinstance(f.feature_target, str) else None
            )
            if target is None:
                err("Target feature must appear earlier in the feature tree.")
            elif isinstance(target, Extrude) and target.operation == "boss":
                sketch = sketches.get(target.sketch)
                min_dimension = _extrude_minimum_dimension(target, sketch) if sketch else None
                if min_dimension is None:
                    issues.append(Issue(
                        "warning", f.name,
                        "Cannot derive a conservative size limit for this target extrusion; "
                        "SolidWorks will validate the modifier against its actual topology.",
                    ))
                elif f.radius_mm > min_dimension / 2:
                    err(
                        f"Modifier size {f.radius_mm:g} mm exceeds half the smallest "
                        f"bounding dimension ({min_dimension:g} mm) of target extrusion '{target.name}'."
                    )
            elif target is not None:
                issues.append(Issue(
                    "warning", f.name,
                    "Target is not a supported blind boss extrusion for bounding-dimension preflight; "
                    "SolidWorks will validate the modifier against its actual topology.",
                ))
            if not has_body:
                err("An edge modifier needs an existing solid body.")
        elif isinstance(f, WeldmentMember):
            def err(msg, f=f):
                issues.append(Issue("error", f.name, msg))

            if f.sketch_3d_name not in sketches_3d:
                err("Needs a 3D sketch path that appears earlier in the tree.")
            else:
                used_sketches.add(f.sketch_3d_name)
                if not sketches_3d[f.sketch_3d_name].segments:
                    err("The referenced 3D sketch has no line segments.")
            if not isinstance(f.profile_path, str) or not os.path.isabs(f.profile_path):
                err("Weldment profile path must be absolute.")
            elif not f.profile_path.lower().endswith(".sldlfp"):
                err("Weldment profile must be a .sldlfp file.")
            elif not os.path.isfile(f.profile_path):
                err(f"Weldment profile does not exist: '{f.profile_path}'.")
            if f.corner_treatment not in (1, 2, 3):
                err("Corner treatment must be 1 (Miter), 2 (Butt1), or 3 (Butt2).")
            has_body = True

        prior_features[f.name] = f

    for name, sk in sketches.items():
        if name not in used_sketches and sk.entities:
            issues.append(Issue("warning", name, "Sketch is not used by any feature."))
    for name, sk in sketches_3d.items():
        if name not in used_sketches and sk.segments:
            issues.append(Issue("warning", name, "3D sketch is not used by any weldment member."))
    if not any(isinstance(f, (Extrude, Revolve, WeldmentMember)) for f in part.features):
        issues.append(Issue("warning", "", "No solid features yet."))
    return issues


def has_errors(issues: List[Issue]) -> bool:
    return any(i.level == "error" for i in issues)
