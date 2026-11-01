"""Deterministic raster drawing analysis for mixed/native PDF pages."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np


def _normalized_line_segments(lines: np.ndarray | None) -> np.ndarray:
    """Normalize OpenCV Hough line output to an array shaped (N, 4)."""
    if lines is None:
        return np.empty((0, 4), dtype=np.int32)

    values = np.asarray(lines)
    if values.size == 0:
        return np.empty((0, 4), dtype=np.int32)

    try:
        values = values.reshape(-1, 4)
    except ValueError:
        return np.empty((0, 4), dtype=np.int32)

    return values.astype(np.int32, copy=False)


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


def _line_orientation_scores(lines: np.ndarray | None) -> tuple[float, float, int]:
    normalized = _normalized_line_segments(lines)
    if len(normalized) == 0:
        return 0.0, 0.0, 0
    orth = 0
    iso = 0
    total = 0
    for x0, y0, x1, y1 in normalized:
        length = float(np.hypot(x1 - x0, y1 - y0))
        if length < 8:
            continue
        angle = abs(float(np.degrees(np.arctan2(y1 - y0, x1 - x0)))) % 180.0
        if min(abs(angle), abs(angle - 90.0), abs(angle - 180.0)) <= 12.0:
            orth += 1
        if min(abs(angle - 30.0), abs(angle - 150.0)) <= 15.0:
            iso += 1
        total += 1
    if total == 0:
        return 0.0, 0.0, 0
    return orth / total, iso / total, total


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
    for x0, y0, x1, y1 in _normalized_line_segments(lines):
        angle = abs(float(np.degrees(np.arctan2(y1 - y0, x1 - x0)))) % 180.0
        if min(abs(angle - 45.0), abs(angle - 135.0)) > 18.0:
            continue
        if float(np.hypot(x1 - x0, y1 - y0)) < min(gray.shape[:2]) * 0.2:
            continue
        cv2.line(cleaned, (int(x0), int(y0)), (int(x1), int(y1)), 255, 3)
        watermark_candidates.append({
            "type": "line_segment",
            "pixels": [[int(x0), int(y0)], [int(x1), int(y1)]],
            "classification": "watermark_candidate",
            "confidence": None,
            "heuristic_score": 0.62,
            "confidence_status": "uncalibrated",
            "evidence": ["long-diagonal-faint-line", f"angle:{angle:.1f}"],
        })
    return cleaned, watermark_candidates


def _detect_view_regions(binary: np.ndarray) -> list[tuple[int, int, int, int]]:
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    grouped = cv2.dilate(binary, kernel, iterations=1)
    count, _, stats, _ = cv2.connectedComponentsWithStats(grouped, connectivity=8)
    boxes: list[tuple[int, int, int, int]] = []
    image_area = float(binary.shape[0] * binary.shape[1])
    for index in range(1, count):
        x, y, w, h, area = stats[index]
        if area < max(250.0, image_area * 0.005) or w < 20 or h < 20:
            continue
        boxes.append((int(x), int(y), int(w), int(h)))
    boxes.sort(key=lambda item: item[0])
    return boxes


def _near_circular_outline_ratio(
    binary_roi: np.ndarray,
    minimum_axis_px: int,
) -> float | None:
    contours, _ = cv2.findContours(
        binary_roi,
        cv2.RETR_LIST,
        cv2.CHAIN_APPROX_NONE,
    )
    min_axis = max(
        40,
        minimum_axis_px,
        round(min(binary_roi.shape[:2]) * 0.45),
    )
    roi_area = float(binary_roi.shape[0] * binary_roi.shape[1])
    for contour in contours:
        if len(contour) < 5 or cv2.contourArea(contour) == 0:
            continue
        try:
            _, axes, _ = cv2.fitEllipse(contour)
        except cv2.error:
            continue
        major_axis = max(axes)
        minor_axis = min(axes)
        if major_axis <= 0 or minor_axis < min_axis:
            continue
        ratio = float(minor_axis / major_axis)
        ellipse_area = float(np.pi * major_axis * minor_axis / 4.0)
        if ratio >= 0.86 and ellipse_area / roi_area >= 0.18:
            return ratio
    return None


def _classify_views(
    gray: np.ndarray,
    view_boxes: list[tuple[int, int, int, int]],
    binary_image: np.ndarray | None = None,
) -> list[dict[str, Any]]:
    classified: list[dict[str, Any]] = []
    rightmost_index = max(range(len(view_boxes)), key=lambda idx: view_boxes[idx][0], default=-1)
    has_multiple_candidates = len(view_boxes) > 1
    if binary_image is None:
        binary_image = cv2.threshold(
            gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU
        )[1]
    for index, (x, y, w, h) in enumerate(view_boxes):
        roi = gray[y:y + h, x:x + w]
        binary_roi = binary_image[y:y + h, x:x + w]
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
        circular_outline_ratio = _near_circular_outline_ratio(
            binary_roi,
            round(min(gray.shape[:2]) * 0.15),
        )
        aspect_ratio = max(w / max(h, 1), h / max(w, 1))
        page_scale_container = (
            has_multiple_candidates
            and w >= gray.shape[1] * 0.85
            and h >= gray.shape[0] * 0.85
        )
        recognition_method = "hough_line_orientation_heuristic"
        if page_scale_container:
            view_type = "unknown"
            confidence = 0.3
            modeling_role = "insufficient_evidence"
            evidence = ["page-scale-container"]
        elif aspect_ratio > 8.0:
            view_type = "unknown"
            confidence = 0.3
            modeling_role = "insufficient_evidence"
            evidence = ["extreme-aspect-region"]
        elif orth_score >= 0.55 and orth_score >= iso_score + 0.15:
            view_type = "orthographic"
            confidence = min(0.95, 0.55 + orth_score * 0.4)
            modeling_role = "candidate_model_source"
            evidence = ["axis-aligned-line-dominance", f"orth_score:{orth_score:.2f}"]
        elif circular_outline_ratio is not None:
            view_type = "orthographic"
            confidence = 0.78
            modeling_role = "candidate_model_source"
            recognition_method = "contour_ellipse_and_line_orientation_heuristic"
            evidence = [
                "near-circular-projected-outline",
                f"ellipse_axis_ratio:{circular_outline_ratio:.2f}",
            ]
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
            "confidence": None,
            "heuristic_score": round(float(confidence), 3),
            "confidence_status": "uncalibrated",
            "classification_status": "candidate_requires_review",
            "recognition_method": recognition_method,
            "evidence": evidence + [f"line_count:{line_count}"],
        })
    return classified


def _box_contains(box: tuple[int, int, int, int], x: int, y: int) -> bool:
    left, top, width, height = box
    return left <= x <= left + width and top <= y <= top + height


def _write_orthographic_views_png(
    source_image: np.ndarray,
    view_regions: list[dict[str, Any]],
    crop_offset: dict[str, int],
    analysis_scale: float,
    output_png: str | Path,
) -> dict[str, Any] | None:
    """Write padded crops of orthographic candidates, excluding other view types."""
    orthographic_regions = [
        region for region in view_regions
        if region.get("classification") == "orthographic"
    ]
    if not orthographic_regions:
        return None

    source_height, source_width = source_image.shape[:2]
    crop_padding = 24
    tiles = []
    extracted_regions = []
    sorted_regions = sorted(
        orthographic_regions,
        key=lambda item: (
            item["bbox_px"]["y"],
            item["bbox_px"]["x"],
        ),
    )
    for region in sorted_regions:
        box = region["bbox_px"]
        left = (box["x"] + crop_offset["left"]) / analysis_scale
        top = (box["y"] + crop_offset["top"]) / analysis_scale
        width = box["width"] / analysis_scale
        height = box["height"] / analysis_scale
        right = left + width
        bottom = top + height
        margin_left = max(24, round(width * 0.15))
        margin_right = margin_left
        margin_top = max(24, round(height * 0.15))
        margin_bottom = margin_top

        for other in view_regions:
            if other is region:
                continue
            other_box = other["bbox_px"]
            other_left = (other_box["x"] + crop_offset["left"]) / analysis_scale
            other_top = (other_box["y"] + crop_offset["top"]) / analysis_scale
            other_right = other_left + other_box["width"] / analysis_scale
            other_bottom = other_top + other_box["height"] / analysis_scale
            horizontal_overlap = left < other_right and other_left < right
            vertical_overlap = top < other_bottom and other_top < bottom
            if vertical_overlap and other_right <= left:
                margin_left = min(margin_left, max(0, round(left - other_right - 1)))
            elif vertical_overlap and other_left >= right:
                margin_right = min(margin_right, max(0, round(other_left - right - 1)))
            if horizontal_overlap and other_bottom <= top:
                margin_top = min(margin_top, max(0, round(top - other_bottom - 1)))
            elif horizontal_overlap and other_top >= bottom:
                margin_bottom = min(
                    margin_bottom,
                    max(0, round(other_top - bottom - 1)),
                )

        x0 = max(0, round(left - margin_left))
        y0 = max(0, round(top - margin_top))
        x1 = min(source_width, round(right + margin_right))
        y1 = min(source_height, round(bottom + margin_bottom))
        if x1 <= x0 or y1 <= y0:
            continue
        tiles.append(source_image[y0:y1, x0:x1].copy())
        extracted_regions.append({
            "view_id": region["view_id"],
            "bbox_source_px": {
                "x": x0,
                "y": y0,
                "width": x1 - x0,
                "height": y1 - y0,
            },
            "padding_ratio": 0.15,
        })

    if not tiles:
        return None

    canvas_width = max(tile.shape[1] for tile in tiles)
    canvas_height = sum(tile.shape[0] for tile in tiles) + crop_padding * (len(tiles) + 1)
    canvas = np.full((canvas_height, canvas_width), 255, dtype=np.uint8)
    y = crop_padding
    for tile in tiles:
        x = (canvas_width - tile.shape[1]) // 2
        canvas[y:y + tile.shape[0], x:x + tile.shape[1]] = tile
        y += tile.shape[0] + crop_padding

    output_path = Path(output_png)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output_path), canvas):
        raise OSError(f"Could not write orthographic view image: {output_path}")
    return {
        "path": str(output_path),
        "view_count": len(tiles),
        "layout": "separate padded view crops stacked vertically",
        "regions": extracted_regions,
    }


def _write_drawing_layout_png(
    source_image: np.ndarray,
    view_regions: list[dict[str, Any]],
    crop_offset: dict[str, int],
    analysis_scale: float,
    output_png: str | Path,
) -> dict[str, Any] | None:
    """Preserve the complete source drawing image and its original view layout."""
    source_height, source_width = source_image.shape[:2]
    output_path = Path(output_png)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output_path), source_image):
        raise OSError(f"Could not write drawing layout image: {output_path}")
    return {
        "path": str(output_path),
        "view_count": len(view_regions),
        "layout": "original source image preserved without rearrangement or crop",
        "source_image_preserved": True,
        "regions": [
            {
                "view_id": region["view_id"],
                "classification": region["classification"],
                "bbox_source_px": {
                    "x": round(
                        (region["bbox_px"]["x"] + crop_offset["left"])
                        / max(float(analysis_scale), 1e-9)
                    ),
                    "y": round(
                        (region["bbox_px"]["y"] + crop_offset["top"])
                        / max(float(analysis_scale), 1e-9)
                    ),
                    "width": round(
                        region["bbox_px"]["width"]
                        / max(float(analysis_scale), 1e-9)
                    ),
                    "height": round(
                        region["bbox_px"]["height"]
                        / max(float(analysis_scale), 1e-9)
                    ),
                },
            }
            for region in view_regions
        ],
        "crop_bbox_source_px": {
            "x": 0,
            "y": 0,
            "width": source_width,
            "height": source_height,
        },
    }


def analyze_raster_drawing(
    gray_image: np.ndarray,
    *,
    output_png: str | Path | None = None,
    orthographic_views_png: str | Path | None = None,
    drawing_layout_png: str | Path | None = None,
    extraction: dict[str, Any] | None = None,
    preserve_full_frame: bool = False,
) -> dict[str, Any]:
    """Analyze a drawing raster using deterministic image-processing heuristics."""
    if gray_image.ndim != 2 or gray_image.size == 0:
        return {"status": "error", "note": "Analyzer requires a non-empty grayscale image."}

    source_image = gray_image.copy()
    source_height, source_width = gray_image.shape
    max_dimension = 1200
    analysis_scale = min(1.0, max_dimension / max(source_width, source_height))
    if analysis_scale < 1.0:
        gray_image = cv2.resize(
            gray_image,
            (
                max(1, round(source_width * analysis_scale)),
                max(1, round(source_height * analysis_scale)),
            ),
            interpolation=cv2.INTER_AREA,
        )

    scan_preprocessing_applied = source_width * source_height >= 2_000_000
    if scan_preprocessing_applied:
        background = cv2.GaussianBlur(
            gray_image,
            (0, 0),
            sigmaX=max(gray_image.shape) / 40.0,
            sigmaY=max(gray_image.shape) / 40.0,
        )
        gray_image = cv2.divide(gray_image, background, scale=255)
        gray_image = cv2.medianBlur(gray_image, 3)

    full_frame = gray_image.copy()
    cropped, crop_offset = _crop_to_ink(gray_image)
    if scan_preprocessing_applied:
        cleaned = cropped
        watermark_candidates: list[dict[str, Any]] = []
        binary = cv2.adaptiveThreshold(
            cleaned,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV,
            35,
            7,
        )
    else:
        cleaned, watermark_candidates = _suppress_diagonal_watermark(cropped)
        binary = cv2.threshold(
            cleaned, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU
        )[1]
    view_boxes = _detect_view_regions(binary)
    view_regions = _classify_views(cleaned, view_boxes, binary)
    orthographic_output = None
    if orthographic_views_png is not None:
        orthographic_output = _write_orthographic_views_png(
            source_image,
            view_regions,
            crop_offset,
            analysis_scale,
            orthographic_views_png,
        )
    drawing_layout_output = None
    if drawing_layout_png is not None:
        drawing_layout_output = _write_drawing_layout_png(
            source_image,
            view_regions,
            crop_offset,
            analysis_scale,
            drawing_layout_png,
        )

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
    for segment in _normalized_line_segments(line_segments):
        x0, y0, x1, y1 = map(int, segment)
        cx = int(round((x0 + x1) / 2.0))
        cy = int(round((y0 + y1) / 2.0))
        in_orthographic = any(_box_contains(box, cx, cy) for box in orthographic_boxes)
        angle = abs(float(np.degrees(np.arctan2(y1 - y0, x1 - x0)))) % 180.0
        axis_distance = min(abs(angle), abs(angle - 90.0), abs(angle - 180.0))
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
            "confidence": None,
            "heuristic_score": confidence,
            "confidence_status": "uncalibrated",
            "recognition_method": "line_orientation_and_view_location_heuristic",
            "evidence": evidence + [f"angle:{angle:.1f}"],
        })

    for box in orthographic_boxes:
        x, y, width, height = box
        roi = cleaned[y:y + height, x:x + width]
        roi_scale = min(1.0, 700 / max(roi.shape[:2]))
        if roi_scale < 1.0:
            roi = cv2.resize(
                roi,
                (
                    max(1, round(width * roi_scale)),
                    max(1, round(height * roi_scale)),
                ),
                interpolation=cv2.INTER_AREA,
            )
        circles = cv2.HoughCircles(
            roi,
            cv2.HOUGH_GRADIENT,
            dp=1.2,
            minDist=max(18, int(min(roi.shape[:2]) * 0.08)),
            param1=120,
            param2=20,
            minRadius=4,
            maxRadius=max(8, int(min(roi.shape[:2]) * 0.25)),
        )
        if circles is not None:
            for circle in np.round(circles[0, :]).astype(int):
                roi_x, roi_y, roi_radius = map(int, circle)
                cx = x + int(round(roi_x / roi_scale))
                cy = y + int(round(roi_y / roi_scale))
                radius = int(round(roi_radius / roi_scale))
                candidates.append({
                    "type": "circle",
                    "center_px": [cx, cy],
                    "radius_px": radius,
                    "classification": "hole_candidate",
                    "confidence": None,
                    "heuristic_score": 0.76,
                    "confidence_status": "uncalibrated",
                    "recognition_method": "hough_circle_candidate",
                    "evidence": ["hough-circle", "inside-orthographic-view"],
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
                    "confidence": None,
                    "heuristic_score": 0.44,
                    "confidence_status": "uncalibrated",
                    "recognition_method": "small_component_outside_view",
                    "evidence": ["small-component", "outside-orthographic-view"],
                })

    candidates.extend(watermark_candidates)
    if output_png is not None:
        canvas = cv2.cvtColor(
            full_frame if preserve_full_frame else cleaned,
            cv2.COLOR_GRAY2BGR,
        )
        crop_x = crop_offset["left"] if preserve_full_frame else 0
        crop_y = crop_offset["top"] if preserve_full_frame else 0
        for region in view_regions:
            box = region["bbox_px"]
            color = (0, 180, 0) if region["classification"] == "orthographic" else (180, 120, 0)
            left = box["x"] + crop_x
            top = box["y"] + crop_y
            cv2.rectangle(
                canvas,
                (left, top),
                (left + box["width"], top + box["height"]),
                color,
                2,
            )
            label = region["view_id"]
            (label_width, label_height), baseline = cv2.getTextSize(
                label,
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                2,
            )
            label_x = min(max(left + 4, 0), max(canvas.shape[1] - label_width - 8, 0))
            label_y = min(max(top + 4, 0), max(canvas.shape[0] - label_height - baseline - 8, 0))
            cv2.rectangle(
                canvas,
                (label_x, label_y),
                (label_x + label_width + 8, label_y + label_height + baseline + 8),
                (35, 35, 35),
                -1,
            )
            cv2.putText(
                canvas,
                label,
                (label_x + 4, label_y + label_height + 4),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )
        output_path = Path(output_png)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(output_path), canvas)

    orthographic_count = sum(1 for region in view_regions if region["classification"] == "orthographic")
    limitations = [
        "Deterministic raster heuristics only; no OCR or AI vision is used.",
        "Dimension and text regions are candidate detections, not recovered numeric values.",
        "No complete geometry reconstruction is attempted in this stage.",
    ]
    if scan_preprocessing_applied:
        limitations.append(
            "High-resolution scan regions and features are heuristic candidates; "
            "review them manually before using them as geometry."
        )

    return {
        "status": "ok",
        "crop_offset_px": crop_offset,
        "source_size_px": {"width": source_width, "height": source_height},
        "analysis_scale": round(float(analysis_scale), 6),
        "scan_preprocessing_applied": scan_preprocessing_applied,
        "overlay_preserves_full_frame": bool(preserve_full_frame),
        "orthographic_views_png": (
            orthographic_output["path"] if orthographic_output else None
        ),
        "orthographic_extracted_view_count": (
            orthographic_output["view_count"] if orthographic_output else 0
        ),
        "orthographic_extraction": orthographic_output,
        "drawing_views_png": (
            drawing_layout_output["path"] if drawing_layout_output else None
        ),
        "drawing_view_layout": drawing_layout_output,
        "modeling_source_policy": "orthographic_only",
        "modeling_source_note": (
            "Isometric/reference views are never treated as modeling sources by this analyzer. "
            "Only orthographic candidates are marked for future reconstruction."
        ),
        "limitations": limitations,
        "extraction": extraction or {},
        "view_regions": view_regions,
        "orthographic_view_count": orthographic_count,
        "candidate_features": candidates,
        "overlay_png": str(output_png) if output_png is not None else None,
    }
