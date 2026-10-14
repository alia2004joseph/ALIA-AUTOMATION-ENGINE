"""Deterministic native-PDF drawing parsing and role classification."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class PdfDrawingParseError(ValueError):
    """A PDF could not be parsed for deterministic drawing diagnostics."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


ROLE_GEOMETRY = "geometry"
ROLE_DIMENSION = "dimension"
ROLE_ANNOTATION = "annotation"
ROLE_CENTERLINE = "centerline"
ROLE_HIDDEN_LINE = "hidden-line"
ROLE_UNKNOWN = "unknown"

_DIMENSION_TEXT_RE = re.compile(
    r"(?:[Ø⌀∅R]\s*\d)|(?:\d+(?:\.\d+)?\s*(?:mm|°))|(?:\bTHRU\b|\bDEEP\b|\bC'BORE\b|\bC'SINK\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DrawingEntity:
    entity_id: str
    kind: str
    role: str
    bbox_pt: dict[str, float] | None
    source: dict[str, Any]
    geometry: dict[str, Any]
    evidence: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "kind": self.kind,
            "role": self.role,
            "bbox_pt": self.bbox_pt,
            "source": self.source,
            "geometry": self.geometry,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class DrawingPage:
    page_number: int
    width_pt: float
    height_pt: float
    rotation_degrees: int
    content_type: str
    processing_route: str
    route_reason: str
    has_native_strokes: bool
    native_word_count: int
    raster_image_bbox_pt: tuple[float, float, float, float] | None
    vector_entities: list[dict[str, Any]]
    embedded_images: list[dict[str, Any]]
    native_text: list[dict[str, Any]]
    entities: list[DrawingEntity]

    def to_dict(self, *, include_entities: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "page_number": self.page_number,
            "width_pt": self.width_pt,
            "height_pt": self.height_pt,
            "rotation_degrees": self.rotation_degrees,
            "content_type": self.content_type,
            "processing_route": self.processing_route,
            "route_reason": self.route_reason,
            "native_text": self.native_text,
            "vector_entities": self.vector_entities,
            "embedded_images": self.embedded_images,
        }
        if include_entities:
            payload["classified_entities"] = [entity.to_dict() for entity in self.entities]
        return payload


@dataclass(frozen=True)
class DrawingDocument:
    source_file: str
    pages: list[DrawingPage]

    def to_report(self, *, include_entities: bool = False) -> dict[str, Any]:
        role_counts: dict[str, int] = {}
        for page in self.pages:
            for entity in page.entities:
                role_counts[entity.role] = role_counts.get(entity.role, 0) + 1
        return {
            "status": "ok",
            "source_file": self.source_file,
            "coordinate_system": {
                "unit": "PDF points",
                "origin": "page top-left",
                "points_per_inch": 72,
            },
            "page_count": len(self.pages),
            "pages": [
                page.to_dict(include_entities=include_entities)
                for page in self.pages
            ],
            "summary": {
                "vector_entity_count": sum(len(page.vector_entities) for page in self.pages),
                "embedded_image_count": sum(len(page.embedded_images) for page in self.pages),
                "native_word_count": sum(len(page.native_text) for page in self.pages),
                "content_types": sorted({page.content_type for page in self.pages}),
                "processing_routes": sorted({page.processing_route for page in self.pages}),
                "classified_role_counts": dict(sorted(role_counts.items())),
            },
        }


def _bbox(element: dict[str, Any]) -> dict[str, float] | None:
    keys = ("x0", "top", "x1", "bottom")
    if not all(key in element for key in keys):
        return None
    return {key: float(element[key]) for key in keys}


def _content_type(has_vectors: bool, has_images: bool, has_text: bool) -> str:
    component_count = sum((has_vectors, has_images, has_text))
    if component_count > 1:
        return "mixed"
    if has_vectors:
        return "vector"
    if has_images:
        return "raster"
    if has_text:
        return "text-only"
    return "empty"


def _route(content_type: str, vector_count: int, image_count: int) -> tuple[str, str]:
    if vector_count:
        if image_count:
            return (
                "vector-first-with-raster-review",
                "Vector geometry is present, so inspect it directly; embedded images "
                "may contain additional drawing content.",
            )
        return (
            "vector-first",
            "Vector geometry is present; avoid raster rediscovery of those entities.",
        )
    if image_count:
        return (
            "raster-preprocessing",
            "No vector geometry was found and embedded image content is present; "
            "inspect the image via a raster path.",
        )
    if content_type == "text-only":
        return "native-text-review", "The page contains native text but no vector geometry or images."
    return "no-geometry-found", "No native text, vector geometry, or embedded images were found."


def _dash_segments(dash: Any) -> list[float]:
    if dash is None:
        return []
    if isinstance(dash, (tuple, list)) and dash:
        candidate = dash[0] if isinstance(dash[0], (list, tuple)) else dash
        return [
            float(value)
            for value in candidate
            if isinstance(value, (int, float)) and float(value) > 0
        ]
    return []


def _style_role(dash_segments: list[float]) -> tuple[str | None, list[str]]:
    if not dash_segments:
        return None, []
    evidence = [f"dash-pattern:{','.join(f'{value:g}' for value in dash_segments)}"]
    if len(dash_segments) >= 2 and max(dash_segments) >= min(dash_segments) * 2.0:
        return ROLE_CENTERLINE, evidence + ["long-short dash pattern"]
    return ROLE_HIDDEN_LINE, evidence


def _distance_to_bbox(point: tuple[float, float], bbox: dict[str, float]) -> float:
    px, py = point
    nearest_x = min(max(px, bbox["x0"]), bbox["x1"])
    nearest_y = min(max(py, bbox["top"]), bbox["bottom"])
    return math.hypot(px - nearest_x, py - nearest_y)


def _line_angle(line: tuple[tuple[float, float], tuple[float, float]]) -> float:
    (x0, y0), (x1, y1) = line
    return math.degrees(math.atan2(y1 - y0, x1 - x0))


def _angle_delta(a: float, b: float) -> float:
    delta = abs((a - b) % 180.0)
    return min(delta, 180.0 - delta)


def _is_near_border(rect: dict[str, float], page_width: float, page_height: float) -> bool:
    margin = max(page_width, page_height) * 0.03
    area = max(0.0, rect["x1"] - rect["x0"]) * max(0.0, rect["bottom"] - rect["top"])
    page_area = page_width * page_height
    return (
        rect["x0"] <= margin
        and rect["top"] <= margin
        and rect["x1"] >= page_width - margin
        and rect["bottom"] >= page_height - margin
        and area >= page_area * 0.6
    )


def _is_title_block_like(
    rect: dict[str, float],
    page_width: float,
    page_height: float,
    text_boxes: list[dict[str, float]],
) -> bool:
    width = max(0.0, rect["x1"] - rect["x0"])
    height = max(0.0, rect["bottom"] - rect["top"])
    area = width * height
    page_area = page_width * page_height
    if not (page_area * 0.01 <= area <= page_area * 0.35):
        return False
    if rect["x0"] > page_width * 0.45 and rect["top"] > page_height * 0.45:
        overlap_count = 0
        for box in text_boxes:
            if (
                box["x0"] >= rect["x0"]
                and box["x1"] <= rect["x1"]
                and box["top"] >= rect["top"]
                and box["bottom"] <= rect["bottom"]
            ):
                overlap_count += 1
        return overlap_count >= 2
    return False


def _has_arrow_like_support(
    line_index: int,
    line_segments: list[tuple[tuple[float, float], tuple[float, float]]],
    line_lengths: list[float],
) -> bool:
    segment = line_segments[line_index]
    base_angle = _line_angle(segment)
    endpoints = segment
    for endpoint in endpoints:
        supports = 0
        for candidate_index, candidate in enumerate(line_segments):
            if candidate_index == line_index:
                continue
            length = line_lengths[candidate_index]
            if not (2.0 <= length <= 14.0):
                continue
            candidate_points = candidate
            if min(math.dist(endpoint, candidate_points[0]), math.dist(endpoint, candidate_points[1])) > 2.5:
                continue
            delta = _angle_delta(base_angle, _line_angle(candidate))
            if 20.0 <= delta <= 80.0:
                supports += 1
        if supports:
            return True
    return False


def parse_pdf_drawing(pdf_path: str | Path) -> DrawingDocument:
    """Parse native PDF entities into deterministic drawing roles."""
    path = Path(pdf_path)
    if not path.is_file():
        raise PdfDrawingParseError("PDF_READ_ERROR", f"PDF file does not exist: {path}")
    if path.suffix.lower() != ".pdf":
        raise PdfDrawingParseError("PDF_READ_ERROR", "Input must be a PDF file.")

    try:
        import pdfplumber
    except ImportError as exc:
        raise PdfDrawingParseError(
            "PDF_READ_ERROR",
            "pdfplumber is required for PDF inspection; install project requirements.",
        ) from exc

    pages: list[DrawingPage] = []
    try:
        with pdfplumber.open(path) as document:
            if not document.pages:
                raise PdfDrawingParseError("PDF_READ_ERROR", "The PDF contains no pages.")
            for page_index, page in enumerate(document.pages):
                text_words = []
                text_boxes = []
                for word_index, word in enumerate(page.extract_words()):
                    bbox = {
                        key: float(word[key])
                        for key in ("x0", "top", "x1", "bottom")
                    }
                    text_words.append({"text": word["text"], "bbox_pt": bbox})
                    text_boxes.append(bbox)

                images = [
                    {
                        "bbox_pt": _bbox(image),
                        "pixel_width": int(image.get("width", 0)),
                        "pixel_height": int(image.get("height", 0)),
                    }
                    for image in page.images
                ]

                vector_entities = []
                entities: list[DrawingEntity] = []
                line_segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
                line_lengths: list[float] = []
                line_entity_offsets: list[int] = []

                for raw_index, line in enumerate(page.lines):
                    bbox = _bbox(line)
                    start = (float(line["x0"]), float(line["top"]))
                    end = (float(line["x1"]), float(line["bottom"]))
                    style_dash = _dash_segments(line.get("dash"))
                    role, evidence = _style_role(style_dash)
                    role = role or ROLE_GEOMETRY
                    entity = DrawingEntity(
                        entity_id=f"p{page_index + 1}-line-{raw_index + 1}",
                        kind="line",
                        role=role,
                        bbox_pt=bbox,
                        source={"page_number": page_index + 1, "native_index": raw_index},
                        geometry={"endpoints_pt": [list(start), list(end)], "dash": style_dash},
                        evidence=evidence,
                    )
                    entities.append(entity)
                    line_entity_offsets.append(len(entities) - 1)
                    line_segments.append((start, end))
                    line_lengths.append(math.dist(start, end))
                    vector_entities.append({
                        "type": "line",
                        "bbox_pt": bbox,
                        "endpoints_pt": [list(start), list(end)],
                    })

                dimension_text_boxes = [
                    item["bbox_pt"]
                    for item in text_words
                    if _DIMENSION_TEXT_RE.search(item["text"])
                ]
                for line_index, entity_offset in enumerate(line_entity_offsets):
                    entity = entities[entity_offset]
                    if entity.role != ROLE_GEOMETRY:
                        continue
                    midpoint = (
                        (line_segments[line_index][0][0] + line_segments[line_index][1][0]) / 2.0,
                        (line_segments[line_index][0][1] + line_segments[line_index][1][1]) / 2.0,
                    )
                    near_dimension_text = any(
                        _distance_to_bbox(midpoint, box) <= 30.0
                        for box in dimension_text_boxes
                    )
                    has_arrow = _has_arrow_like_support(line_index, line_segments, line_lengths)
                    if near_dimension_text and has_arrow:
                        entities[entity_offset] = DrawingEntity(
                            entity_id=entity.entity_id,
                            kind=entity.kind,
                            role=ROLE_DIMENSION,
                            bbox_pt=entity.bbox_pt,
                            source=entity.source,
                            geometry=entity.geometry,
                            evidence=entity.evidence + ["near-dimension-text", "arrow-like-neighbors"],
                        )

                for raw_index, rect in enumerate(page.rects):
                    bbox = _bbox(rect)
                    width = float(rect["x1"] - rect["x0"])
                    height = float(rect["bottom"] - rect["top"])
                    role = ROLE_GEOMETRY
                    evidence: list[str] = []
                    if bbox and _is_near_border(bbox, float(page.width), float(page.height)):
                        role = ROLE_ANNOTATION
                        evidence.append("page-border-like rectangle")
                    elif bbox and _is_title_block_like(
                        bbox,
                        float(page.width),
                        float(page.height),
                        text_boxes,
                    ):
                        role = ROLE_ANNOTATION
                        evidence.append("title-block-like rectangle")
                    entity = DrawingEntity(
                        entity_id=f"p{page_index + 1}-rectangle-{raw_index + 1}",
                        kind="rectangle",
                        role=role,
                        bbox_pt=bbox,
                        source={"page_number": page_index + 1, "native_index": raw_index},
                        geometry={"width_pt": width, "height_pt": height},
                        evidence=evidence,
                    )
                    entities.append(entity)
                    vector_entities.append({
                        "type": "rectangle",
                        "bbox_pt": bbox,
                        "width_pt": width,
                        "height_pt": height,
                    })

                for raw_index, curve in enumerate(page.curves):
                    bbox = _bbox(curve)
                    style_dash = _dash_segments(curve.get("dash"))
                    role, evidence = _style_role(style_dash)
                    role = role or ROLE_GEOMETRY
                    path = curve.get("path") if isinstance(curve, dict) else None
                    entity = DrawingEntity(
                        entity_id=f"p{page_index + 1}-curve-{raw_index + 1}",
                        kind="curve",
                        role=role,
                        bbox_pt=bbox,
                        source={"page_number": page_index + 1, "native_index": raw_index},
                        geometry={"path_segment_count": len(path) if path else 0, "dash": style_dash},
                        evidence=evidence,
                    )
                    entities.append(entity)
                    vector_entities.append({
                        "type": "curve",
                        "bbox_pt": bbox,
                        "path_segment_count": len(path) if path else 0,
                    })

                for raw_index, word in enumerate(text_words):
                    text = word["text"]
                    role = ROLE_DIMENSION if _DIMENSION_TEXT_RE.search(text) else ROLE_ANNOTATION
                    evidence = ["dimension-text-token"] if role == ROLE_DIMENSION else []
                    entities.append(DrawingEntity(
                        entity_id=f"p{page_index + 1}-text-{raw_index + 1}",
                        kind="text",
                        role=role,
                        bbox_pt=word["bbox_pt"],
                        source={"page_number": page_index + 1, "native_index": raw_index},
                        geometry={"text": text},
                        evidence=evidence,
                    ))

                entities = sorted(
                    entities,
                    key=lambda item: (
                        item.kind,
                        item.entity_id,
                        item.bbox_pt["x0"] if item.bbox_pt else math.inf,
                        item.bbox_pt["top"] if item.bbox_pt else math.inf,
                    ),
                )

                has_native_strokes = bool(page.lines or page.curves)
                raster_image = max(
                    page.images,
                    key=lambda image: image["width"] * image["height"],
                    default=None,
                )
                has_images = bool(images)
                content_type = _content_type(bool(vector_entities), has_images, bool(text_words))
                route, reason = _route(content_type, len(vector_entities), len(images))
                raster_bbox = None
                if raster_image is not None:
                    raster_bbox = (
                        float(raster_image["x0"]),
                        float(raster_image["top"]),
                        float(raster_image["x1"]),
                        float(raster_image["bottom"]),
                    )
                pages.append(DrawingPage(
                    page_number=page_index + 1,
                    width_pt=float(page.width),
                    height_pt=float(page.height),
                    rotation_degrees=int(getattr(page, "rotation", 0) or 0),
                    content_type=content_type,
                    processing_route=route,
                    route_reason=reason,
                    has_native_strokes=has_native_strokes,
                    native_word_count=len(text_words),
                    raster_image_bbox_pt=raster_bbox,
                    vector_entities=vector_entities,
                    embedded_images=images,
                    native_text=text_words,
                    entities=entities,
                ))
    except PdfDrawingParseError:
        raise
    except Exception as exc:
        raise PdfDrawingParseError(
            "PDF_READ_ERROR", f"Could not inspect PDF '{path.name}': {exc}"
        ) from exc
    return DrawingDocument(source_file=path.name, pages=pages)
