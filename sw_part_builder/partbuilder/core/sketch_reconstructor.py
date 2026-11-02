"""Cautious reconstruction of one simple outer profile from a raster view."""
from __future__ import annotations

import math
from typing import Any


class SketchReconstructionError(ValueError):
    """The selected view is not safe to turn into a single-profile sketch."""


def orthographic_reconstruction_candidates(
    raster_report: dict[str, Any],
    interpretation_page: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return raster orthographic candidates not reviewed as another view type."""
    reviewed_types = {
        view.get("view_id"): view.get("review", {}).get("view_type")
        for view in (interpretation_page or {}).get("views", [])
    }
    return [
        region for region in raster_report.get("view_regions", [])
        if region.get("classification") == "orthographic"
        and reviewed_types.get(region.get("view_id")) in (None, "orthographic")
    ]


def reconstruct_raster_profile(
    image,
    raster_report: dict[str, Any],
    view_id: str,
    known_width_mm: float,
) -> dict[str, Any]:
    """Extract one closed outer contour, calibrated by a user-confirmed width.

    Internal loops and multiple disconnected contours are rejected rather than
    silently omitted or converted into misleading sketch geometry.
    """
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise SketchReconstructionError(
            "Raster sketch reconstruction requires OpenCV and NumPy."
        ) from exc

    try:
        known_width_mm = float(known_width_mm)
    except (TypeError, ValueError) as exc:
        raise SketchReconstructionError(
            "Enter a confirmed overall profile width in millimeters."
        ) from exc
    if not math.isfinite(known_width_mm) or known_width_mm <= 0:
        raise SketchReconstructionError(
            "The confirmed profile width must be a positive finite value in millimeters."
        )

    region = next(
        (
            candidate for candidate in raster_report.get("view_regions", [])
            if candidate.get("view_id") == view_id
        ),
        None,
    )
    if region is None:
        raise SketchReconstructionError(f"View '{view_id}' was not found.")
    if region.get("classification") != "orthographic":
        raise SketchReconstructionError(
            "Only a candidate marked orthographic can be used as a sketch source."
        )

    try:
        scale = float(raster_report.get("analysis_scale", 1.0))
        offset = raster_report.get("crop_offset_px") or {}
        box = region["bbox_px"]
        left = (float(box["x"]) + float(offset.get("left", 0))) / scale
        top = (float(box["y"]) + float(offset.get("top", 0))) / scale
        width = float(box["width"]) / scale
        height = float(box["height"]) / scale
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise SketchReconstructionError(
            "The selected view does not have a usable source-image bounding box."
        ) from exc
    if not math.isfinite(scale) or scale <= 0 or width <= 0 or height <= 0:
        raise SketchReconstructionError(
            "The selected view has invalid image coordinates or analysis scale."
        )

    source = np.asarray(image)
    if source.ndim == 2:
        gray = source
    elif source.ndim == 3 and source.shape[2] in (3, 4):
        conversion = cv2.COLOR_RGB2GRAY if source.shape[2] == 3 else cv2.COLOR_RGBA2GRAY
        gray = cv2.cvtColor(source, conversion)
    else:
        raise SketchReconstructionError("The drawing image has an unsupported format.")

    image_height, image_width = gray.shape[:2]
    margin = max(4, round(min(width, height) * 0.06))
    x0 = max(0, math.floor(left - margin))
    y0 = max(0, math.floor(top - margin))
    x1 = min(image_width, math.ceil(left + width + margin))
    y1 = min(image_height, math.ceil(top + height + margin))
    if x1 - x0 < 12 or y1 - y0 < 12:
        raise SketchReconstructionError(
            "The selected view is too small or outside the source image."
        )

    crop = gray[y0:y1, x0:x1]
    threshold = cv2.threshold(
        crop, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU
    )[1]
    contours, hierarchy = cv2.findContours(
        threshold,
        cv2.RETR_TREE,
        cv2.CHAIN_APPROX_SIMPLE,
    )
    if hierarchy is None:
        raise SketchReconstructionError("No closed profile was found in this view.")

    contour_tree = hierarchy[0]
    crop_area = float(crop.shape[0] * crop.shape[1])
    significant_area = max(20.0, crop_area * 0.002)
    external = [
        index for index, contour in enumerate(contours)
        if contour_tree[index][3] == -1
        and cv2.contourArea(contour) >= significant_area
    ]
    if not external:
        raise SketchReconstructionError(
            "No sufficiently large closed outer profile was found."
        )
    primary_index = max(external, key=lambda index: cv2.contourArea(contours[index]))
    primary = contours[primary_index]
    primary_area = float(cv2.contourArea(primary))
    additional = [
        index for index in external
        if index != primary_index
        and cv2.contourArea(contours[index]) >= max(significant_area, primary_area * 0.01)
    ]
    if additional:
        raise SketchReconstructionError(
            "This view contains multiple separate outlines. Select a simpler view; "
            "automatic profile selection is not supported yet."
        )

    children = []
    child = contour_tree[primary_index][2]
    while child != -1:
        children.append(child)
        child = contour_tree[child][0]
    while children:
        child = children.pop()
        nested = contour_tree[child][2]
        while nested != -1:
            children.append(nested)
            nested = contour_tree[nested][0]
        child_area = float(cv2.contourArea(contours[child]))
        if child_area >= max(significant_area, primary_area * 0.002) and (
            child_area / primary_area < 0.8
        ):
            raise SketchReconstructionError(
                "An internal loop or opening was detected. This first version only "
                "reconstructs a single outer profile; holes are not silently discarded."
            )

    px, py, profile_width_px, profile_height_px = cv2.boundingRect(primary)
    if (
        px <= 1
        or py <= 1
        or px + profile_width_px >= crop.shape[1] - 1
        or py + profile_height_px >= crop.shape[0] - 1
    ):
        raise SketchReconstructionError(
            "The detected profile touches the view crop boundary and may be clipped."
        )
    if profile_width_px < 8 or profile_height_px < 8:
        raise SketchReconstructionError("The detected profile is too small to trace.")

    perimeter = float(cv2.arcLength(primary, True))
    approximation = cv2.approxPolyDP(
        primary,
        max(1.0, perimeter * 0.01),
        True,
    )
    if len(approximation) < 3 or len(approximation) > 64:
        raise SketchReconstructionError(
            "The contour is too complex for this first sketch-reconstruction version."
        )

    contour_points = approximation.reshape(-1, 2).astype(float)
    min_x = float(contour_points[:, 0].min())
    max_x = float(contour_points[:, 0].max())
    min_y = float(contour_points[:, 1].min())
    max_y = float(contour_points[:, 1].max())
    actual_width_px = max_x - min_x
    if actual_width_px <= 0:
        raise SketchReconstructionError("The detected profile has no measurable width.")
    mm_per_pixel = known_width_mm / actual_width_px
    center_x = (min_x + max_x) / 2.0
    center_y = (min_y + max_y) / 2.0
    points_mm = [
        [
            round((float(x) - center_x) * mm_per_pixel, 5),
            round((center_y - float(y)) * mm_per_pixel, 5),
        ]
        for x, y in contour_points
    ]

    circularity = (
        4.0 * math.pi * primary_area / (perimeter * perimeter)
        if perimeter > 0
        else 0.0
    )
    aspect_ratio = profile_width_px / profile_height_px
    if circularity >= 0.85 and 0.9 <= aspect_ratio <= 1.1:
        average_diameter = (
            profile_width_px + profile_height_px
        ) / 2.0 * mm_per_pixel
        shape = {
            "type": "circle",
            "cx": 0.0,
            "cy": 0.0,
            "diameter": round(average_diameter, 5),
        }
    else:
        shape = {"type": "polygon", "points": points_mm}

    preview = cv2.cvtColor(crop, cv2.COLOR_GRAY2RGB)
    shifted = approximation.copy()
    cv2.polylines(
        preview,
        [shifted],
        True,
        (0, 170, 45),
        max(2, round(min(preview.shape[:2]) / 300)),
        cv2.LINE_AA,
    )

    return {
        "view_id": view_id,
        "shape": shape,
        "profile_type": shape["type"],
        "vertex_count": len(points_mm) if shape["type"] == "polygon" else 0,
        "width_mm": known_width_mm,
        "height_mm": round(profile_height_px * mm_per_pixel, 5),
        "mm_per_pixel": mm_per_pixel,
        "calibration": "user_confirmed_overall_width",
        "preview_rgb": preview,
    }
