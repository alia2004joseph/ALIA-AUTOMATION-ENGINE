"""Extract dimension evidence stored natively in DXF files."""
from pathlib import Path
from typing import Any

GEOMETRY_TYPES = {"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "ELLIPSE", "SPLINE"}

DIMENSION_TYPES = {
    0: "linear",
    1: "aligned",
    2: "angular",
    3: "diameter",
    4: "radius",
    5: "angular_3_point",
    6: "ordinate",
}
ANGULAR_TYPES = {"angular", "angular_3_point"}


class DxfDimensionError(ValueError):
    """A DXF dimension report cannot be produced reliably."""


def inspect_dxf_dimensions(file_path: str | Path) -> dict[str, Any]:
    """Read native DXF dimensions and account for document units and DIMLFAC.

    Values are dimension metadata from the DXF, not measurements estimated
    from rendered pixels. The report deliberately does not associate dimensions
    with 3D features.
    """
    path = Path(file_path)
    if not path.is_file():
        raise DxfDimensionError(f"DXF file does not exist: {path}")
    if path.suffix.lower() != ".dxf":
        raise DxfDimensionError("Input must be a DXF file.")

    try:
        import ezdxf
        from ezdxf import units
    except ImportError as exc:
        raise DxfDimensionError("DXF inspection requires ezdxf.") from exc

    try:
        document = ezdxf.readfile(str(path))
        modelspace = document.modelspace()
    except (OSError, ValueError, ezdxf.DXFError) as exc:
        raise DxfDimensionError(f"Could not read DXF: {exc}") from exc

    if not document.units:
        return {
            "status": "unresolved_units",
            "source_file": path.name,
            "units": None,
            "dimension_count": 0,
            "dimensions": [],
            "note": "DXF INSUNITS is unset; physical dimension units cannot be confirmed.",
        }

    try:
        unit_to_mm = float(units.conversion_factor(document.units, units.MM))
    except (ValueError, TypeError) as exc:
        raise DxfDimensionError(
            f"DXF insertion units could not be converted to millimetres: {exc}"
        ) from exc

    dimensions = []
    for entity in modelspace.query("DIMENSION"):
        try:
            raw_type = int(entity.dxf.dimtype) & 0x0F
            kind = DIMENSION_TYPES.get(raw_type, "unknown")
            style_name = entity.dxf.dimstyle
            style = document.dimstyles.get(style_name)
            dimlfac = float(style.dxf.get("dimlfac", 1.0)) if style else 1.0
            measurement = float(entity.get_measurement())
            factor = 1.0 if kind in ANGULAR_TYPES else dimlfac
            value = measurement * factor
            if kind not in ANGULAR_TYPES:
                value *= unit_to_mm

            dimension = {
                "handle": entity.dxf.handle,
                "type": kind,
                "raw_type": raw_type,
                "raw_measurement": measurement,
                "dimlfac": dimlfac,
                "units_factor_to_mm": 1.0 if kind in ANGULAR_TYPES else unit_to_mm,
                "value": value,
                "unit": "degree" if kind in ANGULAR_TYPES else "mm",
                "dimension_style": style_name,
                "text_override": str(entity.dxf.get("text", "")),
                "layer": entity.dxf.get("layer", "0"),
            }
            for field in ("defpoint", "defpoint2", "defpoint3", "defpoint4"):
                if entity.dxf.hasattr(field):
                    point = entity.dxf.get(field)
                    dimension[field] = [float(point.x), float(point.y)]
            dimensions.append(dimension)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            dimensions.append({
                "handle": entity.dxf.get("handle"),
                "type": "unresolved",
                "error": str(exc),
                "text_override": str(entity.dxf.get("text", "")),
                "layer": entity.dxf.get("layer", "0"),
            })

    return {
        "status": "ok",
        "source_file": path.name,
        "units": units.unit_name(document.units),
        "dimension_count": len(dimensions),
        "dimensions": dimensions,
        "note": (
            "Native dimension values are extracted from DXF entities. Their "
            "association with a model feature or projected view is not inferred."
        ),
    }


def format_dimension_evidence(report: dict[str, Any]) -> list[str]:
    """Format extracted dimension evidence for the application's log panel."""
    lines = []
    for dimension in report.get("dimensions", []):
        if "value" not in dimension:
            lines.append(
                f"  {dimension.get('handle', '?')}: unresolved "
                f"({dimension.get('error', 'unsupported dimension')})"
            )
            continue
        value = dimension["value"]
        unit = dimension["unit"]
        formatted = f"{value:g} {unit}"
        suffix = (
            f" | text: {dimension['text_override']}"
            if dimension.get("text_override") not in ("", "<>")
            else ""
        )
        lines.append(
            f"  {dimension['handle']}: {dimension['type']} {formatted}{suffix}"
        )
    return lines


def analyze_dxf_drawing(file_path: str | Path) -> dict[str, Any]:
    """Summarize DXF drawing content and spatially cluster candidate views.

    Clusters are geometric regions, not confirmed projection views. The report
    intentionally leaves view orientation and dimension-to-feature matching
    unresolved instead of treating annotations or sheet borders as part edges.
    """
    path = Path(file_path)
    if not path.is_file() or path.suffix.lower() != ".dxf":
        raise DxfDimensionError(f"DXF file does not exist or is not a DXF: {path}")

    try:
        import ezdxf
        from ezdxf import bbox
    except ImportError as exc:
        raise DxfDimensionError("DXF analysis requires ezdxf.") from exc

    try:
        document = ezdxf.readfile(str(path))
        modelspace = document.modelspace()
        entity_counts: dict[str, int] = {}
        for entity in modelspace:
            kind = entity.dxftype()
            entity_counts[kind] = entity_counts.get(kind, 0) + 1

        records = []
        for entity in modelspace:
            if entity.dxftype() not in GEOMETRY_TYPES:
                continue
            try:
                box = bbox.extents([entity], fast=False)
            except (ArithmeticError, AttributeError, TypeError, ValueError):
                continue
            if not box.has_data:
                continue
            records.append({
                "handle": entity.dxf.handle,
                "type": entity.dxftype(),
                "min_x": float(box.extmin.x),
                "min_y": float(box.extmin.y),
                "max_x": float(box.extmax.x),
                "max_y": float(box.extmax.y),
            })
    except (OSError, ValueError, ezdxf.DXFError) as exc:
        raise DxfDimensionError(f"Could not analyze DXF geometry: {exc}") from exc

    if records:
        min_x = min(item["min_x"] for item in records)
        min_y = min(item["min_y"] for item in records)
        max_x = max(item["max_x"] for item in records)
        max_y = max(item["max_y"] for item in records)
        width, height = max_x - min_x, max_y - min_y
    else:
        min_x = min_y = max_x = max_y = width = height = 0.0

    # Keep the tolerance local enough to avoid chaining distant orthographic
    # views together. Long entities are not excluded: a full-height edge can
    # be genuine part geometry, and DXF alone does not identify its role.
    threshold = max(2.0, ((width * width + height * height) ** 0.5) * 0.025)
    clustered = []
    for item in records:
        clustered.append(item)

    parents = list(range(len(clustered)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for left_index, left in enumerate(clustered):
        for right_index in range(left_index + 1, len(clustered)):
            right = clustered[right_index]
            dx = max(
                left["min_x"] - right["max_x"],
                right["min_x"] - left["max_x"],
                0.0,
            )
            dy = max(
                left["min_y"] - right["max_y"],
                right["min_y"] - left["max_y"],
                0.0,
            )
            if (dx * dx + dy * dy) ** 0.5 <= threshold:
                union(left_index, right_index)

    groups: dict[int, list[dict[str, Any]]] = {}
    for index, item in enumerate(clustered):
        groups.setdefault(find(index), []).append(item)

    regions = []
    unassigned_entity_count = 0
    sheet_scale_entity_count = 0
    for items in groups.values():
        if len(items) < 3:
            unassigned_entity_count += len(items)
            continue
        region_min_x = min(item["min_x"] for item in items)
        region_min_y = min(item["min_y"] for item in items)
        region_max_x = max(item["max_x"] for item in items)
        region_max_y = max(item["max_y"] for item in items)
        if (
            width > 0
            and height > 0
            and region_max_x - region_min_x >= width * 0.85
            and region_max_y - region_min_y >= height * 0.85
        ):
            sheet_scale_entity_count += len(items)
            continue
        region = {
            "min_x": region_min_x,
            "min_y": region_min_y,
            "max_x": region_max_x,
            "max_y": region_max_y,
            "entity_count": len(items),
            "entity_types": {},
            "entity_handles": [item["handle"] for item in items],
        }
        region["width"] = region["max_x"] - region["min_x"]
        region["height"] = region["max_y"] - region["min_y"]
        for item in items:
            kind = item["type"]
            region["entity_types"][kind] = region["entity_types"].get(kind, 0) + 1
        regions.append(region)
    regions.sort(key=lambda region: (region["min_x"], region["min_y"]))
    for index, region in enumerate(regions, start=1):
        region["candidate_id"] = f"region-{index:02d}"

    dimension_report = inspect_dxf_dimensions(path)
    for dimension in dimension_report["dimensions"]:
        points = [
            dimension[field]
            for field in ("defpoint2", "defpoint3", "defpoint4", "defpoint")
            if field in dimension
        ]
        if not points or not regions:
            dimension["nearest_region_candidate"] = None
            continue
        point_x = sum(point[0] for point in points) / len(points)
        point_y = sum(point[1] for point in points) / len(points)
        distances = []
        for region in regions:
            dx = max(region["min_x"] - point_x, point_x - region["max_x"], 0.0)
            dy = max(region["min_y"] - point_y, point_y - region["max_y"], 0.0)
            distances.append((dx * dx + dy * dy) ** 0.5)
        closest = min(range(len(distances)), key=distances.__getitem__)
        dimension["nearest_region_candidate"] = {
            "candidate_id": regions[closest]["candidate_id"],
            "distance_drawing_units": distances[closest],
            "association_confirmed": False,
        }

    return {
        "status": "ok",
        "source_file": path.name,
        "document_units": dimension_report["units"],
        "entity_counts": dict(sorted(entity_counts.items())),
        "geometry_extents_drawing_units": {
            "min_x": min_x,
            "min_y": min_y,
            "max_x": max_x,
            "max_y": max_y,
            "width": width,
            "height": height,
        },
        "clustering_tolerance_drawing_units": threshold,
        "sheet_scale_entities_excluded_from_regions": sheet_scale_entity_count,
        "low_support_entities_not_assigned_to_regions": unassigned_entity_count,
        "candidate_regions": regions,
        "dimensions": dimension_report["dimensions"],
        "interpretation": {
            "projection_convention": "unresolved",
            "view_orientations": "unresolved",
            "dimension_feature_associations": "unconfirmed spatial candidates only",
            "model_generation": "not attempted",
        },
    }


def format_dxf_analysis(report: dict[str, Any]) -> list[str]:
    """Return concise, explicitly tentative interpretation diagnostics."""
    counts = report["entity_counts"]
    count_summary = ", ".join(f"{kind}={count}" for kind, count in counts.items())
    lines = [
        f"[DXF] Native entities: {count_summary or 'none'}; "
        f"units={report['document_units'] or 'unresolved'}.",
        f"[DXF] Found {len(report['candidate_regions'])} candidate geometry "
        "region(s); regions are not automatically assigned to projection views.",
    ]
    for region in report["candidate_regions"]:
        lines.append(
            f"  {region['candidate_id']}: "
            f"{region['width']:.3g} x {region['height']:.3g} drawing units, "
            f"{region['entity_count']} geometry entities."
        )
    lines.extend(format_dimension_evidence(report))
    lines.append(
        "[DXF] Projection convention, view orientation, and dimension-to-feature "
        "associations remain unconfirmed; this analysis report does not itself "
        "create a 3D model."
    )
    return lines
