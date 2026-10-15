"""Deterministic raster drawing analysis for mixed/native PDF pages."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np


def _crop_to_ink(gray: np.ndarray, margin: int = 6) -> tuple[np.ndarray, dict[str, int]]:
    if gray.size == 0:
        return gray, {"left": 0, "top": 0}
    _, inv = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    ys, xs = np.where(inv > 0)
    if ys.size == 0 or xs.size == 0:
        return gray, {"left": 0, "top": 0}
    top = max(int(ys.min()) - margin, 0)
    left = max(int(xs.min()) - margin, 0)
    bottom = min(int(ys.max()) + margin + 1, gray.shape[0])
    right = min(int(xs.max()) + margin + 1, gray.shape[1])
    return gray[top:bottom, left:right].copy(), {"left": left, "top": top}


def _suppress_diagonal_watermark(gray: np.ndarray) -> tuple[np.ndarray, list[dict[str, Any]]]:
    normalized = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
    strong = cv2.threshold(normalized, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    faint = cv2.adaptiveThreshold(
        normalized,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        35,
        5,
    )
    candidates = cv2.subtract(faint, strong)
    lines = cv2.HoughLinesP(
        candidates,
        1,
        np.pi / 180.0,
        threshold=40,
        minLineLength=max(40, int(min(gray.shape[:2]) * 0.25)),
        maxLineGap=8,
    )
    cleaned = normalized.copy()
    watermark_candidates: list[dict[str, Any]] = []
    if lines is None:
        return cleaned, watermark_candidates
    for line in lines[:, 0]:
        x0, y0, x1, y1 = map(int, line)
        dx = x1 - x0
        dy = y1 - y0
        length = float(np.hypot(dx, dy))
        if length < max(40.0, min(gray.shape[:2]) * 0.25):
            continue
        angle = abs(float(np.degrees(np.arctan2(dy, dx))))
        angle = min(angle, 180.0 - angle)
        if 20.0 <= angle <= 70.0:
            cv2.line(cleaned, (x0, y0), (x1, y1), 255, 2)
            watermark_candidates.append({
                "type": "line_segment",
                "pixels": [[x0, y0], [x1, y1]],
                "classification": "watermark_candidate",
                "confidence": 0.65,
                "evidence": ["diagonal-low-contrast-line", f"angle:{angle:.1f}"],
            })
    return cleaned, watermark_candidates


def _line_orientation_scores(lines: np.ndarray | None) -> tuple[float, float, int]:
    if lines is None or not len(lines):
        return 0.0, 0.0, 0
    orth = 0
    iso = 0
    total = 0
    for line in lines[:, 0]:
        x0, y0, x1, y1 = line
        length = float(np.hypot(x1 - x0, y1 - y0))
        if length < 12:
            continue
        total += 1
        angle = abs(float(np.degrees(np.arctan2(y1 - y0, x1 - x0))))
        angle = angle % 180.0
        axis_distance = min(abs(angle - 0.0), abs(angle - 90.0), abs(angle - 180.0))
        iso_distance = min(abs(angle - 30.0), abs(angle - 60.0), abs(angle - 120.0), abs(angle - 150.0))
        if axis_distance <= 12.0:
            orth += 1
        if iso_distance <= 12.0:
            iso += 1
    if total == 0:
        return 0.0, 0.0, 0
    return orth / total, iso / total, total


def _detect_view_regions(binary: np.ndarray) -> list[tuple[int, int, int, int]]:
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
    grouped = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    grouped = cv2.dilate(grouped, kernel, iterations=1)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(grouped, connectivity=8)
    boxes: list[tuple[int, int, int, int]] = []
    image_area = float(binary.shape[0] * binary.shape[1])
    for index in range(1, count):
        x, y, w, h, area = stats[index]
        if area < max(250.0, image_area * 0.005):
            continue
        if w < 20 or h < 20:
            continue
        boxes.append((int(x), int(y), int(w), int(h)))
    boxes.sort(key=lambda item: item[0])
    return boxes


def _classify_views(gray: np.ndarray, view_boxes: list[tuple[int, int, int, int]]) -> list[dict[str, Any]]:
    classified: list[dict[str, Any]] = []
    rightmost_index = max(range(len(view_boxes)), key=lambda idx: view_boxes[idx][0], default=-1)
    for index, (x, y, w, h) in enumerate(view_boxes):
        roi = gray[y:y + h, x:x + w]
        edges = cv2.Canny(roi, 60, 180)
        lines = cv2.HoughLinesP(
            edges,
            1,
            np.pi / 180.0,
            threshold=25,
            minLineLength=max(16, int(min(w, h) * 0.12)),
            maxLineGap=6,
        )
        orth_score, iso_score, line_count = _line_orientation_scores(lines)
        if orth_score >= 0.55 and orth_score >= iso_score + 0.15:
            view_type = "orthographic"
            confidence = min(0.95, 0.55 + orth_score * 0.4)
            modeling_role = "candidate_model_source"
            evidence = ["axis-aligned-line-dominance", f"orth_score:{orth_score:.2f}"]
        elif iso_score >= 0.40 and iso_score >= orth_score:
            view_type = "isometric_reference"
            confidence = min(0.9, 0.45 + iso_score * 0.4)
            modeling_role = "reference_only"
            evidence = ["isometric-angle-clusters", f"iso_score:{iso_score:.2f}"]
        elif index == rightmost_index and iso_score >= 0.20:
            view_type = "isometric_reference"
            confidence = 0.45
            modeling_role = "reference_only"
            evidence = ["rightmost-view-heuristic", f"iso_score:{iso_score:.2f}"]
        else:
            view_type = "unknown"
            confidence = 0.3
            modeling_role = "insufficient_evidence"
            evidence = ["weak-orientation-signal"]
        classified.append({
            "view_id": f"view-{index + 1:02d}",
            "bbox_px": {"x": x, "y": y, "width": w, "height": h},
            "classification": view_type,
            "modeling_role": modeling_role,
            "confidence": round(float(confidence), 3),
            "evidence": evidence + [f"line_count:{line_count}"],
        })
    return classified


def _box_contains(box: tuple[int, int, int, int], x: int, y: int) -> bool:
    left, top, width, height = box
    return left <= x <= left + width and top <= y <= top + height


def analyze_raster_drawing(
    gray_image: np.ndarray,
    *,
    output_png: str | Path | None = None,
    extraction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Analyze a drawing raster using deterministic image-processing heuristics."""
    if gray_image.ndim != 2 or gray_image.size == 0:
        return {"status": "error", "note": "Analyzer requires a non-empty grayscale image."}

    cropped, crop_offset = _crop_to_ink(gray_image)
    cleaned, watermark_candidates = _suppress_diagonal_watermark(cropped)
    binary = cv2.threshold(cleaned, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    view_boxes = _detect_view_regions(binary)
    view_regions = _classify_views(cleaned, view_boxes)

    edges = cv2.Canny(cleaned, 60, 180)
    line_segments = cv2.HoughLinesP(
        edges, 1, np.pi / 180.0, threshold=35,
        minLineLength=max(20, int(min(cleaned.shape[:2]) * 0.08)),
        maxLineGap=8,
    )
    orthographic_boxes = [
        (
            region["bbox_px"]["x"],
            region["bbox_px"]["y"],
            region["bbox_px"]["width"],
            region["bbox_px"]["height"],
        )
        for region in view_regions
        if region["classification"] == "orthographic"
    ]
    candidates: list[dict[str, Any]] = []
    if line_segments is not None:
        for segment in line_segments[:, 0]:
            x0, y0, x1, y1 = map(int, segment)
            cx = int(round((x0 + x1) / 2.0))
            cy = int(round((y0 + y1) / 2.0))
            in_orthographic = any(_box_contains(box, cx, cy) for box in orthographic_boxes)
            angle = abs(float(np.degrees(np.arctan2(y1 - y0, x1 - x0)))) % 180.0
            axis_distance = min(abs(angle - 0.0), abs(angle - 90.0), abs(angle - 180.0))
            if in_orthographic and axis_distance <= 15.0:
                label = "profile_geometry"
                confidence = 0.72
                evidence = ["in-orthographic-view", "near-axis-line"]
            elif not in_orthographic and axis_distance <= 15.0:
                label = "dimension_annotation"
                confidence = 0.48
                evidence = ["outside-model-view", "axis-line"]
            else:
                label = "unknown"
                confidence = 0.3
                evidence = ["line-unclassified"]
            candidates.append({
                "type": "line_segment",
                "pixels": [[x0, y0], [x1, y1]],
                "classification": label,
                "confidence": confidence,
                "evidence": evidence + [f"angle:{angle:.1f}"],
            })

    circles = cv2.HoughCircles(
        cleaned,
        cv2.HOUGH_GRADIENT,
        dp=1.2,
        minDist=max(18, int(min(cleaned.shape[:2]) * 0.08)),
        param1=120,
        param2=20,
        minRadius=4,
        maxRadius=max(8, int(min(cleaned.shape[:2]) * 0.25)),
    )
    if circles is not None:
        for circle in np.round(circles[0, :]).astype(int):
            cx, cy, radius = map(int, circle)
            in_orthographic = any(_box_contains(box, cx, cy) for box in orthographic_boxes)
            candidates.append({
                "type": "circle",
                "center_px": [cx, cy],
                "radius_px": radius,
                "classification": "hole_candidate" if in_orthographic else "unknown",
                "confidence": 0.76 if in_orthographic else 0.38,
                "evidence": ["hough-circle", "inside-orthographic-view" if in_orthographic else "outside-orthographic-view"],
            })

    text_mask = cv2.morphologyEx(binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 1)))
    text_mask = cv2.dilate(text_mask, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)), iterations=1)
    contours, _ = cv2.findContours(text_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        area = w * h
        if area < 20 or area > cleaned.shape[0] * cleaned.shape[1] * 0.01:
            continue
        aspect = w / max(h, 1)
        if 0.15 <= aspect <= 12.0 and h <= max(40, int(cleaned.shape[0] * 0.09)):
            center_x = x + w // 2
            center_y = y + h // 2
            if not any(_box_contains(box, center_x, center_y) for box in orthographic_boxes):
                candidates.append({
                    "type": "text_region",
                    "bbox_px": [x, y, w, h],
                    "classification": "dimension_annotation",
                    "confidence": 0.44,
                    "evidence": ["small-component", "outside-orthographic-view"],
                })

    candidates.extend(watermark_candidates)
    if output_png is not None:
        canvas = cv2.cvtColor(cleaned, cv2.COLOR_GRAY2BGR)
        for region in view_regions:
            box = region["bbox_px"]
            color = (0, 180, 0) if region["classification"] == "orthographic" else (180, 120, 0)
            cv2.rectangle(
                canvas,
                (box["x"], box["y"]),
                (box["x"] + box["width"], box["y"] + box["height"]),
                color,
                2,
            )
        for candidate in candidates:
            label = candidate["classification"]
            color = {
                "profile_geometry": (0, 255, 0),
                "dimension_annotation": (0, 200, 255),
                "hole_candidate": (255, 180, 0),
                "watermark_candidate": (0, 0, 255),
            }.get(label, (180, 180, 180))
            if candidate["type"] == "line_segment":
                (x0, y0), (x1, y1) = candidate["pixels"]
                cv2.line(canvas, (x0, y0), (x1, y1), color, 1)
            elif candidate["type"] == "circle":
                cx, cy = candidate["center_px"]
                cv2.circle(canvas, (cx, cy), int(candidate["radius_px"]), color, 2)
            elif "bbox_px" in candidate:
                x, y, w, h = candidate["bbox_px"]
                cv2.rectangle(canvas, (x, y), (x + w, y + h), color, 1)
        output_path = Path(output_png)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(output_path), canvas)

    orthographic_count = sum(1 for region in view_regions if region["classification"] == "orthographic")
    return {
        "status": "ok",
        "crop_offset_px": crop_offset,
        "modeling_source_policy": "orthographic_only",
        "modeling_source_note": (
            "Isometric/reference views are never treated as modeling sources by this analyzer. "
            "Only orthographic candidates are marked for future reconstruction."
        ),
        "limitations": [
            "Deterministic raster heuristics only; no OCR or AI vision is used.",
            "Dimension and text regions are candidate detections, not recovered numeric values.",
            "No complete geometry reconstruction is attempted in this stage.",
        ],
        "extraction": extraction or {},
        "view_regions": view_regions,
        "orthographic_view_count": orthographic_count,
        "candidate_features": candidates,
        "overlay_png": str(output_png) if output_png is not None else None,
    }
