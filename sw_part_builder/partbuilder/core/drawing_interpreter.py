"""Deterministic interpretation of native drawing annotations and view candidates."""
from __future__ import annotations

import math
import re
from typing import Any

from .engineering_knowledge import SYMBOL_DEFINITIONS


_DIAMETER = re.compile(r"(?:Ø|⌀|%%c)\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)
_REPEAT_COUNT = re.compile(r"(?P<count>\d+)\s*[x×]\s*(?=(?:Ø|⌀|%%c))", re.IGNORECASE)
_RADIUS = re.compile(r"(?<![A-Z])R\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)
_THREAD = re.compile(
    r"\bM\s*(\d+(?:[.,]\d+)?)(?:\s*[x×]\s*(\d+(?:[.,]\d+)?))?\b",
    re.IGNORECASE,
)
_ANGLE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:°|DEG)(?![A-Z])", re.IGNORECASE)
_LINEAR = re.compile(
    r"(?<![A-Z0-9])(?P<nominal>[+-]?\d+(?:[.,]\d+)?)"
    r"(?:\s*(?:±|\+/-)\s*(?P<symmetric_tolerance>\d+(?:[.,]\d+)?)"
    r"|\s*\+\s*(?P<upper_tolerance>\d+(?:[.,]\d+)?)"
    r"\s*/\s*-\s*(?P<lower_tolerance>\d+(?:[.,]\d+)?))?"
    r"\s*(?P<unit>MM|INCHES|IN)?\b",
    re.IGNORECASE,
)
_SECTION = re.compile(
    r"\b(?:SECTION|SECT\.?)\s+([A-Z0-9]+(?:\s*[-–]\s*[A-Z0-9]+)?)\b",
    re.IGNORECASE,
)
_DETAIL = re.compile(r"\bDETAIL\s+([A-Z0-9]+)\b", re.IGNORECASE)
_PROJECTION = re.compile(r"\b(FIRST|THIRD)\s+ANGLE(?:\s+PROJECTION)?\b", re.IGNORECASE)
_SURFACE_TEXTURE = re.compile(r"\b(RA|RZ)\s*(\d+(?:[.,]\d+)?)\b", re.IGNORECASE)


def _text_lines(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[list[dict[str, Any]]] = []
    def bbox(word: dict[str, Any]) -> dict[str, Any]:
        return word.get("bbox_pt") or word.get("bbox_px") or {}

    for word in sorted(
        words,
        key=lambda item: (bbox(item).get("top", 0), bbox(item).get("x0", 0)),
    ):
        box = bbox(word)
        center_y = (float(box.get("top", 0)) + float(box.get("bottom", 0))) / 2
        row = next(
            (
                candidate
                for candidate in rows
                if abs(candidate[0]["center_y"] - center_y) <= 2.5
            ),
            None,
        )
        item = {"word": word, "center_y": center_y}
        if row is None:
            rows.append([item])
        else:
            row.append(item)

    lines = []
    for row in rows:
        row.sort(key=lambda item: bbox(item["word"])["x0"])
        boxes = [bbox(item["word"]) for item in row]
        text_parts = []
        tokens = []
        cursor = 0
        for item in row:
            token_text = str(item["word"].get("text", ""))
            if text_parts:
                cursor += 1
            start = cursor
            text_parts.append(token_text)
            cursor += len(token_text)
            tokens.append({
                "start": start,
                "end": cursor,
                "text": token_text,
                "bbox": bbox(item["word"]),
            })
        recognition_methods = {
            item["word"].get("recognition_method", "native_pdf_text_pattern")
            for item in row
        }
        coordinate_key = (
            "bbox_px"
            if any("bbox_px" in item["word"] for item in row)
            else "bbox_pt"
        )
        lines.append({
            "text": " ".join(text_parts),
            "tokens": tokens,
            "recognition_method": (
                recognition_methods.pop()
                if len(recognition_methods) == 1
                else "mixed_text_sources"
            ),
            coordinate_key: {
                "x0": min(float(box["x0"]) for box in boxes),
                "top": min(float(box["top"]) for box in boxes),
                "x1": max(float(box["x1"]) for box in boxes),
                "bottom": max(float(box["bottom"]) for box in boxes),
            },
        })
    return lines


def _bbox_for_text_span(
    line: dict[str, Any],
    start: int,
    end: int,
) -> dict[str, float]:
    boxes = [
        token["bbox"]
        for token in line.get("tokens", [])
        if token["start"] < end and start < token["end"]
    ]
    if not boxes:
        return line.get("bbox_px") or line["bbox_pt"]
    return {
        "x0": min(float(box["x0"]) for box in boxes),
        "top": min(float(box["top"]) for box in boxes),
        "x1": max(float(box["x1"]) for box in boxes),
        "bottom": max(float(box["bottom"]) for box in boxes),
    }


def _overlap_ratio(
    first: dict[str, float],
    second: dict[str, float],
) -> float:
    overlap_width = max(
        0.0,
        min(first["x1"], second["x1"]) - max(first["x0"], second["x0"]),
    )
    overlap_height = max(
        0.0,
        min(first["bottom"], second["bottom"]) - max(first["top"], second["top"]),
    )
    overlap_area = overlap_width * overlap_height
    first_area = max(0.0, first["x1"] - first["x0"]) * max(
        0.0, first["bottom"] - first["top"]
    )
    second_area = max(0.0, second["x1"] - second["x0"]) * max(
        0.0, second["bottom"] - second["top"]
    )
    return overlap_area / max(min(first_area, second_area), 1e-9)


def _map_view_bbox_to_page(
    view: dict[str, Any],
    raster: dict[str, Any],
    page: dict[str, Any],
) -> dict[str, float] | None:
    extraction = raster.get("extraction", {})
    source_size = raster.get("source_size_px") or {}
    source_width = float(source_size.get("width", 0))
    source_height = float(source_size.get("height", 0))
    if source_width <= 0 or source_height <= 0:
        return None

    source_bbox = extraction.get("selected_bbox_pt") or {
        "x0": 0.0,
        "top": 0.0,
        "x1": float(page.get("width_pt", 0)),
        "bottom": float(page.get("height_pt", 0)),
    }
    image_width = float(source_bbox["x1"]) - float(source_bbox["x0"])
    image_height = float(source_bbox["bottom"]) - float(source_bbox["top"])
    if image_width <= 0 or image_height <= 0:
        return None

    scale = float(raster.get("analysis_scale", 1.0))
    offset = raster.get("crop_offset_px") or {}
    box = view.get("bbox_px") or {}
    left = (float(box.get("x", 0)) + float(offset.get("left", 0))) / scale
    top = (float(box.get("y", 0)) + float(offset.get("top", 0))) / scale
    right = left + float(box.get("width", 0)) / scale
    bottom = top + float(box.get("height", 0)) / scale
    return {
        "x0": float(source_bbox["x0"]) + left / source_width * image_width,
        "top": float(source_bbox["top"]) + top / source_height * image_height,
        "x1": float(source_bbox["x0"]) + right / source_width * image_width,
        "bottom": float(source_bbox["top"]) + bottom / source_height * image_height,
    }


def _map_source_pixel_bbox_to_page(
    bbox: dict[str, float],
    raster: dict[str, Any],
    page: dict[str, Any],
) -> dict[str, float] | None:
    source_size = raster.get("source_size_px") or {}
    source_width = float(source_size.get("width", 0))
    source_height = float(source_size.get("height", 0))
    if source_width <= 0 or source_height <= 0:
        return None
    extraction = raster.get("extraction", {})
    source_bbox = extraction.get("selected_bbox_pt")
    if source_bbox is None:
        page_width = float(page.get("width_pt", 0))
        page_height = float(page.get("height_pt", 0))
        if page_width <= 0 or page_height <= 0:
            return None
        source_bbox = {
            "x0": 0.0,
            "top": 0.0,
            "x1": page_width,
            "bottom": page_height,
        }
    page_image_width = float(source_bbox["x1"]) - float(source_bbox["x0"])
    page_image_height = float(source_bbox["bottom"]) - float(source_bbox["top"])
    if page_image_width <= 0 or page_image_height <= 0:
        return None
    return {
        "x0": float(source_bbox["x0"]) + bbox["x0"] / source_width * page_image_width,
        "top": float(source_bbox["top"]) + bbox["top"] / source_height * page_image_height,
        "x1": float(source_bbox["x0"]) + bbox["x1"] / source_width * page_image_width,
        "bottom": (
            float(source_bbox["top"])
            + bbox["bottom"] / source_height * page_image_height
        ),
    }


def _distance_to_box(x: float, y: float, box: dict[str, float]) -> float:
    left = float(box.get("x0", box.get("x", 0)))
    top = float(box.get("top", box.get("y", 0)))
    right = float(box.get("x1", left + float(box.get("width", 0))))
    bottom = float(box.get("bottom", top + float(box.get("height", 0))))
    dx = max(left - x, 0.0, x - right)
    dy = max(top - y, 0.0, y - bottom)
    return math.hypot(dx, dy)


def _candidate_view_link(
    bbox: dict[str, float],
    views: list[dict[str, Any]],
    page: dict[str, Any],
) -> dict[str, Any]:
    center_x = (bbox["x0"] + bbox["x1"]) / 2
    center_y = (bbox["top"] + bbox["bottom"]) / 2
    candidates = [
        (
            _distance_to_box(
                center_x,
                center_y,
                view.get("bbox_pt") or view.get("bbox_px"),
            ),
            view["view_id"],
        )
        for view in views
        if view.get("bbox_pt") is not None or view.get("bbox_px") is not None
    ]
    candidates.sort()
    if not candidates:
        return {
            "candidate_view_id": None,
            "candidate_view_ids": [],
            "association_status": "unassigned",
            "association_method": "no_page_view_regions",
        }

    distance, view_id = min(candidates)
    distance_value = round(distance, 2)
    distance_fields = (
        {"distance": distance_value, "distance_pt": distance_value}
        if page.get("width_pt") is not None
        else {"distance": distance_value, "distance_px": distance_value}
    )
    max_distance = math.hypot(
        float(page.get("width_pt", page.get("width_px", 0))),
        float(page.get("height_pt", page.get("height_px", 0))),
    ) * 0.08
    if distance > max_distance:
        return {
            "candidate_view_id": None,
            "candidate_view_ids": [],
            "association_status": "unassigned",
            "association_method": "nearest_view_outside_association_limit",
        }
    if len(candidates) > 1 and math.isclose(
        candidates[0][0],
        candidates[1][0],
        rel_tol=0.05,
        abs_tol=1.0,
    ):
        return {
            "candidate_view_id": None,
            "candidate_view_ids": [candidate[1] for candidate in candidates[:2]],
            "association_status": "ambiguous",
            "association_method": "multiple_equally_near_view_regions",
            **distance_fields,
        }
    return {
        "candidate_view_id": view_id,
        "candidate_view_ids": [view_id],
        "association_status": "tentative",
        "association_method": "nearest_view_region",
        **distance_fields,
    }


def _projection_convention_evidence(
    lines: list[dict[str, Any]],
) -> dict[str, Any]:
    matches = []
    for line in lines:
        for match in _PROJECTION.finditer(line["text"]):
            is_ocr = line.get("recognition_method") == "offline_tesseract"
            matches.append({
                "convention": match.group(1).lower(),
                "raw_text": match.group(0),
                ("bbox_px" if "bbox_px" in line else "bbox_pt"): (
                    _bbox_for_text_span(line, *match.span())
                ),
                "recognition_method": line.get(
                    "recognition_method", "native_pdf_text_pattern"
                ),
                "status": "ocr_text_candidate" if is_ocr else "text_candidate",
            })
    conventions = {item["convention"] for item in matches}
    if len(conventions) == 1:
        return {
            "candidate": conventions.pop(),
            "status": (
                "ocr_text_candidate"
                if any(item["status"] == "ocr_text_candidate" for item in matches)
                else "text_candidate"
            ),
            "evidence": matches,
            "limitations": [
                "Text evidence is not verification of the drawing's projection symbol."
            ],
        }
    if len(conventions) > 1:
        return {
            "candidate": None,
            "status": "conflicting_evidence",
            "evidence": matches,
            "limitations": ["Conflicting first-angle and third-angle text was found."],
        }
    return {
        "candidate": None,
        "status": "unknown",
        "evidence": [],
        "limitations": ["No projection convention was recognized in native text."],
    }


def _projection_convention(lines: list[dict[str, Any]]) -> str | None:
    return _projection_convention_evidence(lines)["candidate"]


def _infer_view_roles(
    views: list[dict[str, Any]],
    projection_convention: str | None,
) -> tuple[str | None, list[dict[str, Any]]]:
    orthographic = [
        view for view in views
        if view.get("view_type") == "orthographic"
        and isinstance(view.get("bbox_px"), dict)
        and float(view["bbox_px"].get("width", 0)) > 0
        and float(view["bbox_px"].get("height", 0)) > 0
    ]
    if not orthographic:
        return None, []

    def area(view: dict[str, Any]) -> float:
        box = view["bbox_px"]
        return float(box["width"]) * float(box["height"])

    reference = max(orthographic, key=area)
    reference_box = reference["bbox_px"]
    possible_roles = [
        "front_view_candidate",
        "top_view_candidate",
        "bottom_view_candidate",
        "left_side_view_candidate",
        "right_side_view_candidate",
        "rear_view_candidate",
        "parent_view_candidate",
        "derived_view_candidate",
        "unknown_view_candidate",
    ]
    reference["semantic_role"] = "layout_reference_candidate"
    reference["role_confidence"] = None
    reference["role_heuristic_score"] = None
    reference["role_evidence"] = ["largest_region_used_only_as_layout_reference"]
    reference["candidate_roles"] = possible_roles
    reference["alternative_roles"] = possible_roles
    reference["role_candidate"] = {
        "value": "layout_reference_candidate",
        "alternatives": possible_roles,
        "status": "layout_reference_only",
        "evidence": ["largest_region_used_only_as_layout_reference"],
        "assumptions": [],
        "review_required": True,
    }
    reference["classification_status"] = "ambiguous_candidate"
    reference["recognition_method"] = "relative_view_layout_heuristic"
    reference["confidence_status"] = "uncalibrated"
    reference["uncertainty"] = (
        "The most informative principal view cannot be identified from region size alone."
    )

    reference_x = float(reference_box["x"]) + float(reference_box["width"]) / 2
    reference_y = float(reference_box["y"]) + float(reference_box["height"]) / 2
    relationships = []
    for view in orthographic:
        if view is reference:
            continue
        box = view["bbox_px"]
        center_x = float(box["x"]) + float(box["width"]) / 2
        center_y = float(box["y"]) + float(box["height"]) / 2
        dx = center_x - reference_x
        dy = center_y - reference_y
        width_scale = max(min(float(box["width"]), float(reference_box["width"])), 1.0)
        height_scale = max(min(float(box["height"]), float(reference_box["height"])), 1.0)
        x_alignment_error = abs(dx) / width_scale
        y_alignment_error = abs(dy) / height_scale
        vertical_alignment = (
            x_alignment_error <= 0.15
            and abs(dy) >= 0.5 * max(float(box["height"]), float(reference_box["height"]))
        )
        horizontal_alignment = (
            y_alignment_error <= 0.15
            and abs(dx) >= 0.5 * max(float(box["width"]), float(reference_box["width"]))
        )
        aligned = vertical_alignment or horizontal_alignment
        if vertical_alignment and (
            not horizontal_alignment or x_alignment_error <= y_alignment_error
        ):
            axis = "vertical"
            position = "above" if dy < 0 else "below"
            alignment_error = x_alignment_error
        elif horizontal_alignment:
            axis = "horizontal"
            position = "left" if dx < 0 else "right"
            alignment_error = y_alignment_error
        else:
            axis = None
            alignment_error = min(x_alignment_error, y_alignment_error)
            if abs(dy) / max(float(reference_box["height"]), 1.0) >= (
                abs(dx) / max(float(reference_box["width"]), 1.0)
            ):
                position = "above" if dy < 0 else "below"
            elif dx:
                position = "left" if dx < 0 else "right"
            else:
                position = "overlapping"

        relationship = {
            "relationship_id": (
                f"view-relation-{len(relationships) + 1:04d}"
            ),
            "source_view_id": reference.get("view_id"),
            "target_view_id": view.get("view_id"),
            "relationship_type": (
                "orthographic_alignment_candidate"
                if aligned
                else "relative_layout_position_candidate"
            ),
            "status": "tentative" if aligned else "position_only",
            "alignment_axis": axis,
            "relative_position": position,
            "alignment_error_ratio": round(alignment_error, 4),
            "alignment_criteria": {
                "maximum_center_offset_ratio": 0.15,
                "minimum_separation_ratio": 0.5,
            },
            "evidence": (
                [
                    f"{axis}_center_alignment_within_15_percent",
                    "separate_candidate_regions",
                ]
                if aligned
                else ["relative_region_position_only", "view_alignment_not_established"]
            ),
            "assumptions": (
                ["aligned_regions_represent_related_orthographic_views"]
                if aligned
                else []
            ),
            "review_required": True,
        }
        relationships.append(relationship)

        if aligned and axis == "vertical":
            if projection_convention == "first":
                role = "bottom_view_candidate" if position == "above" else "top_view_candidate"
            elif projection_convention == "third":
                role = "top_view_candidate" if position == "above" else "bottom_view_candidate"
            else:
                role = f"{position}_projected_view_candidate"
            alternatives = ["top_view_candidate", "bottom_view_candidate"]
        elif aligned and axis == "horizontal":
            if projection_convention == "first":
                role = (
                    "right_side_view_candidate"
                    if position == "left"
                    else "left_side_view_candidate"
                )
            elif projection_convention == "third":
                role = (
                    "left_side_view_candidate"
                    if position == "left"
                    else "right_side_view_candidate"
                )
            else:
                role = f"{position}_projected_view_candidate"
            alternatives = ["left_side_view_candidate", "right_side_view_candidate"]
        else:
            role = "unknown_view_candidate"
            alternatives = possible_roles

        view["semantic_role"] = role
        view["role_confidence"] = None
        view["role_heuristic_score"] = None
        role_evidence = [
            "relative_position_to_largest_orthographic_layout_reference",
            "bbox_alignment_candidate" if aligned else "bbox_alignment_not_established",
        ]
        assumptions = []
        if aligned:
            assumptions.append("layout_reference_is_principal_view")
            if projection_convention in {"first", "third"}:
                assumptions.append("projection_text_matches_drawing_method")
                relationship["projection_convention_candidate"] = projection_convention
                relationship["projection_role_candidate"] = role
            role_evidence.append(
                f"projection_convention:{projection_convention or 'unknown'}"
            )
        view["role_evidence"] = role_evidence
        view["layout_reference_view_id"] = reference.get("view_id")
        view["candidate_roles"] = [role] + [
            alternative for alternative in alternatives if alternative != role
        ]
        view["alternative_roles"] = view["candidate_roles"][1:]
        view["role_candidate"] = {
            "value": role,
            "alternatives": view["alternative_roles"],
            "status": "tentative" if aligned else "unknown",
            "evidence": role_evidence,
            "assumptions": assumptions,
            "review_required": True,
        }
        view["classification_status"] = (
            "ambiguous_candidate" if aligned else "unknown_candidate"
        )
        view["recognition_method"] = "relative_position_and_bbox_alignment_heuristic"
        view["confidence_status"] = "uncalibrated"
        view["uncertainty"] = (
            "Alignment and position suggest a role only if the layout reference is the "
            "principal view; view identity and projection relationships remain unverified."
            if aligned
            else "Relative position alone does not establish a projection relationship or view role."
        )
    return reference.get("view_id"), relationships


def _dimension_annotations(
    lines: list[dict[str, Any]],
    views: list[dict[str, Any]],
    page: dict[str, Any],
) -> list[dict[str, Any]]:
    annotations = []
    for line in lines:
        text = line["text"]
        consumed: list[tuple[int, int]] = []
        non_dimension_spans = [match.span() for match in _SURFACE_TEXTURE.finditer(text)]
        matches: list[tuple[int, int, str, str, dict[str, Any]]] = []

        for kind, pattern in (
            ("diameter", _DIAMETER),
            ("radius", _RADIUS),
            ("thread", _THREAD),
            ("angle", _ANGLE),
            ("linear", _LINEAR),
        ):
            for match in pattern.finditer(text):
                if kind == "linear" and any(
                    match.start() < end and start < match.end()
                    for start, end in non_dimension_spans
                ):
                    continue
                if any(
                    match.start() < end and start < match.end()
                    for start, end in consumed
                ):
                    continue
                raw = match.group(0).strip()
                if not raw:
                    continue
                if kind in {"diameter", "radius"}:
                    value = match.group(1).replace(",", ".")
                elif kind == "thread":
                    value = match.group(1).replace(",", ".")
                elif kind == "angle":
                    value = match.group(1).replace(",", ".")
                else:
                    value = match.group("nominal").replace(",", ".")
                matches.append((match.start(), match.end(), kind, value, match.groupdict()))
                consumed.append((match.start(), match.end()))

        for start, end, kind, value, _groups in sorted(matches):
            prefix_start = max(0, start - 8)
            repeat = (
                _REPEAT_COUNT.search(text[prefix_start:end])
                if kind == "diameter"
                else None
            )
            if repeat is not None and prefix_start + repeat.end() > start:
                repeat = None
            repeat_count = int(repeat.group("count")) if repeat else None
            annotation_start = prefix_start + repeat.start() if repeat else start
            bbox = _bbox_for_text_span(line, annotation_start, end)
            has_explicit_context = any(
                (_groups.get(key) or "")
                for key in (
                    "unit",
                    "symmetric_tolerance",
                    "upper_tolerance",
                    "lower_tolerance",
                )
            )
            annotation = {
                "annotation_id": f"dimension-{len(annotations) + 1:04d}",
                "kind": "dimension",
                "dimension_type": kind,
                "raw_text": text[annotation_start:end],
                "nominal_value": value,
                "information_status": "directly_observed",
                "interpretation_status": (
                    "weak_text_candidate"
                    if kind == "linear" and not has_explicit_context
                    else "text_candidate"
                ),
                "recognition_evidence": (
                    ["numeric_text_only"]
                    if kind == "linear" and not has_explicit_context
                    else ["dimension_notation_or_explicit_context"]
                ),
                "unit": (
                    "mm"
                    if (_groups.get("unit") or "").upper() == "MM"
                    else "in"
                    if (_groups.get("unit") or "").upper() in {"IN", "INCHES"}
                    else None
                ),
                "coordinate_system": (
                    "pixels; image top-left origin"
                    if "bbox_px" in line
                    else "PDF points; page top-left origin"
                ),
                ("bbox_px" if "bbox_px" in line else "bbox_pt"): bbox,
                "recognition_method": line.get(
                    "recognition_method", "native_pdf_text_pattern"
                ),
                "recognition_confidence": None,
                "recognition_heuristic_score": (
                    0.35 if kind == "linear" and not has_explicit_context
                    else 0.65 if kind == "linear"
                    else 0.8
                ),
                "confidence_status": "uncalibrated",
                "review_required": True,
            }
            if repeat_count is not None:
                annotation["repeat_count"] = repeat_count
            definition_id = {
                "diameter": "dimension.diameter",
                "radius": "dimension.radius",
                "thread": "dimension.thread",
                "angle": "dimension.angle",
            }.get(kind)
            if definition_id:
                annotation["knowledge_definition_id"] = definition_id
            if _groups.get("symmetric_tolerance"):
                annotation["tolerance"] = {
                    "type": "symmetric",
                    "value": _groups["symmetric_tolerance"].replace(",", "."),
                }
            elif _groups.get("upper_tolerance") or _groups.get("lower_tolerance"):
                annotation["tolerance"] = {
                    "type": "limits",
                    "upper": (
                        _groups["upper_tolerance"].replace(",", ".")
                        if _groups.get("upper_tolerance") else None
                    ),
                    "lower": (
                        _groups["lower_tolerance"].replace(",", ".")
                        if _groups.get("lower_tolerance") else None
                    ),
                }
            view_link = _candidate_view_link(bbox, views, page)
            annotation.update(view_link)
            linked_view = next(
                (
                    view for view in views
                    if view.get("view_id") == view_link.get("candidate_view_id")
                ),
                None,
            )
            annotation["candidate_view_role"] = (
                linked_view.get("semantic_role") if linked_view else None
            )
            annotations.append(annotation)

    return annotations


def _symbol_annotations(
    lines: list[dict[str, Any]],
    views: list[dict[str, Any]],
    page: dict[str, Any],
) -> list[dict[str, Any]]:
    annotations = []
    patterns = (
        ("section_view_label", _SECTION),
        ("detail_view_label", _DETAIL),
        ("projection_method_label", _PROJECTION),
        ("surface_texture_text", _SURFACE_TEXTURE),
    )
    for line in lines:
        for kind, pattern in patterns:
            for match in pattern.finditer(line["text"]):
                annotation = {
                    "annotation_id": f"symbol-{len(annotations) + 1:04d}",
                    "kind": kind,
                    "raw_text": match.group(0),
                    "label": match.group(1).upper(),
                    "bbox_px" if "bbox_px" in line else "bbox_pt": (
                        _bbox_for_text_span(line, *match.span())
                    ),
                    "recognition_method": line.get(
                        "recognition_method", "native_pdf_text_pattern"
                    ),
                    "recognition_confidence": None,
                    "recognition_heuristic_score": 0.8,
                    "confidence_status": "uncalibrated",
                    "information_status": "directly_observed",
                    "review_required": True,
                }
                definition_id = {
                    "section_view_label": "drawing.section",
                    "detail_view_label": "drawing.detail",
                    "projection_method_label": "drawing.projection_method",
                    "surface_texture_text": "manufacturing.surface_finish",
                }[kind]
                annotation["knowledge_definition_id"] = definition_id
                annotation["recognition_status"] = (
                    SYMBOL_DEFINITIONS[definition_id].recognition_status
                )
                annotation.update(
                    _candidate_view_link(
                        annotation.get("bbox_pt") or annotation["bbox_px"],
                        views,
                        page,
                    )
                )
                annotations.append(annotation)
    return annotations


def interpret_drawing(report: dict[str, Any]) -> dict[str, Any]:
    """Create reviewable drawing annotations from an existing PDF inspection report."""
    pages = []
    for page in report.get("pages", []):
        raster = page.get("raster_analysis") or {}
        lines = _text_lines(page.get("native_text", []))
        raster_ocr_words = page.get("ocr_text", [])
        native_words = page.get("native_text", [])
        normalized_ocr_words = []
        if raster_ocr_words:
            for word in raster_ocr_words:
                bbox_px = word.get("bbox_px")
                if not bbox_px:
                    continue
                bbox_pt = _map_source_pixel_bbox_to_page(bbox_px, raster, page)
                normalized = {
                    "text": word.get("text", ""),
                    "recognition_method": word.get(
                        "recognition_method", "offline_tesseract"
                    ),
                }
                if bbox_pt is not None:
                    normalized["bbox_pt"] = bbox_pt
                else:
                    normalized["bbox_px"] = bbox_px
                duplicate = any(
                    str(native.get("text", "")).strip().casefold()
                    == str(word.get("text", "")).strip().casefold()
                    and _overlap_ratio(
                        bbox_pt or bbox_px,
                        native.get("bbox_pt", {}),
                    ) >= 0.7
                    for native in native_words
                    if native.get("bbox_pt")
                )
                if not duplicate:
                    normalized_ocr_words.append(normalized)
            lines.extend(_text_lines(normalized_ocr_words))
        projection_evidence = _projection_convention_evidence(lines)
        projection_convention = projection_evidence["candidate"]
        views = []
        for region in raster.get("view_regions", []):
            view = {
                "view_id": region.get("view_id"),
                "view_type": region.get("classification", "unknown"),
                "semantic_role": "isometric_reference_candidate"
                if region.get("classification") == "isometric_reference"
                else "unclassified",
                "confidence": region.get("confidence"),
                "heuristic_score": region.get("heuristic_score"),
                "confidence_status": region.get("confidence_status", "uncalibrated"),
                "bbox_px": region.get("bbox_px"),
                "bbox_pt": _map_view_bbox_to_page(region, raster, page),
                "review_required": True,
                "recognition_method": region.get(
                    "recognition_method", "raster_line_orientation_heuristic"
                ),
                "candidate_roles": (
                    ["isometric_reference_candidate", "unknown_view_candidate"]
                    if region.get("classification") == "isometric_reference"
                    else ["unknown_view_candidate"]
                ),
                "alternative_roles": ["unknown_view_candidate"],
                "role_candidate": {
                    "value": (
                        "isometric_reference_candidate"
                        if region.get("classification") == "isometric_reference"
                        else "unknown_view_candidate"
                    ),
                    "alternatives": ["unknown_view_candidate"],
                    "status": "candidate_requires_review",
                    "evidence": [],
                    "assumptions": [],
                    "review_required": True,
                },
                "classification_status": "candidate_requires_review",
            }
            views.append(view)

        layout_reference_view_id, view_relationships = _infer_view_roles(
            views, projection_convention
        )
        dimensions = _dimension_annotations(lines, views, page)
        symbols = _symbol_annotations(lines, views, page)
        ocr_status = raster.get(
            "offline_ocr",
            page.get("offline_ocr", {"status": "not_requested"}),
        )
        for dimension in dimensions:
            if dimension.get("recognition_method") == "native_pdf_text_pattern":
                continue
            dimension["recognition_confidence"] = None
            dimension["confidence_status"] = "uncalibrated"
            dimension["recognition_heuristic_score"] = None
            dimension["information_status"] = "directly_observed"
            dimension["review_required"] = True
            dimension["interpretation_status"] = (
                "weak_text_candidate"
                if dimension["dimension_type"] == "linear"
                else "ocr_text_candidate"
            )
        for symbol in symbols:
            if symbol.get("recognition_method") == "native_pdf_text_pattern":
                continue
            symbol["recognition_confidence"] = None
            symbol["confidence_status"] = "uncalibrated"
            symbol["recognition_heuristic_score"] = None
            symbol["review_required"] = True
        pages.append({
            "page_number": page.get("page_number"),
            "content_type": page.get("content_type"),
            "coordinate_system": page.get("coordinate_system", report.get("coordinate_system")),
            "projection_convention_candidate": projection_convention,
            "projection_convention": projection_evidence,
            "views": views,
            "layout_reference_view_id": layout_reference_view_id,
            "view_relationships": view_relationships,
            "dimensions": dimensions,
            "symbol_annotations": symbols,
            "interpretation_status": (
                "review_required" if dimensions or symbols or views
                else "insufficient_source_data"
            ),
            "limitations": [
                "Raster-only text is interpreted only when offline OCR is available and requested; OCR remains unverified.",
                "The largest orthographic region is only a layout reference; the most informative principal view is not inferred from size.",
                "Role candidates require the layout reference to be the principal view; alignment uses candidate bounding boxes, not corresponding edge geometry.",
                "First-angle and third-angle role mappings use text candidates only; projection symbols and freely positioned reference-arrow views are not recognized.",
                "View links are tentative nearest-region associations and require confirmation.",
                "Dimensions preserve source text; units, tolerances, and manufacturing meaning are not inferred.",
                "Graphical GD&T, weld, thread, and surface-finish symbols are not semantically interpreted.",
            ],
            "units_evidence": page.get("units_evidence", {
                "candidate": None,
                "status": "unknown",
                "evidence": [],
            }),
            "scale_evidence": page.get("scale_evidence", {
                "candidate": None,
                "status": "unknown",
                "evidence": [],
            }),
            "offline_ocr": ocr_status,
            "vector_entities": page.get("vector_entities", []),
        })

    return {
        "schema_version": "1.1",
        "status": "review_required",
        "source_file": report.get("source_file"),
        "source_document": report.get("source_document", {
            "file_name": report.get("source_file"),
            "input_type": "pdf",
        }),
        "input_type": report.get("input_type", "pdf"),
        "standards_profile": "unspecified; no ISO or ASME assumptions applied",
        "interpretation_method": (
            "deterministic_native_pdf_text_and_existing_view_candidates"
        ),
        "interpretation_methods": [
            "native_pdf_text_pattern_matching",
            "existing_raster_view_candidate_analysis",
            "optional_offline_tesseract_ocr",
            "source_native_dxf_entity_preservation",
        ],
        "coordinate_system": report.get("coordinate_system", {
            "unit": "PDF points",
            "origin": "page top-left",
        }),
        "page_count": len(pages),
        "pages": pages,
        "summary": {
            "view_candidate_count": sum(len(page["views"]) for page in pages),
            "view_relationship_candidate_count": sum(
                len(page["view_relationships"]) for page in pages
            ),
            "dimension_candidate_count": sum(len(page["dimensions"]) for page in pages),
            "symbol_annotation_count": sum(len(page["symbol_annotations"]) for page in pages),
            "requires_human_review": True,
        },
        "limitations": [
            "Vector geometry is preserved as source evidence; it is not automatically mapped to 3D features.",
            "OCR text, when present, is an unverified text candidate and may misread engineering notation.",
            "No missing dimensions, units, scales, or feature depths are inferred.",
        ],
    }
