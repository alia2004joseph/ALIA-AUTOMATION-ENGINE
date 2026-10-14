"""Deterministic contour extraction for raster engineering drawings."""
import logging
import os

import numpy as np

logger = logging.getLogger(__name__)
_ORTHOGONAL_TOLERANCE = np.tan(np.deg2rad(5.0))


def convert_raster_image_to_vectors(
    image_path,
    target_dpi=300,
    target_bounds_mm=None,
):
    """Return closed raster contours as sketch-compatible shape dictionaries.

    Pixel dimensions are converted using ``target_dpi``; this assumes the
    drawing was rendered or scanned at its intended 1:1 print scale.
    """
    if not os.path.exists(image_path):
        logger.warning("Raster drawing does not exist: %s", image_path)
        return []
    try:
        import cv2
    except ImportError:
        logger.warning("OpenCV is required for raster vectorization (pip install opencv-python-headless).")
        return []

    image = cv2.imread(os.fspath(image_path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        logger.warning("OpenCV could not decode raster drawing: %s", image_path)
        return []
    return _convert_grayscale_array(image, target_dpi, cv2, target_bounds_mm)


def convert_raster_array_to_vectors(
    image,
    target_dpi=300,
    target_bounds_mm=None,
):
    """Convert a grayscale or RGB/RGBA NumPy image into closed contours."""
    try:
        import cv2
    except ImportError:
        logger.warning("OpenCV is required for raster vectorization (pip install opencv-python-headless).")
        return []

    pixels = np.asarray(image)
    if pixels.ndim == 3:
        conversion = cv2.COLOR_RGBA2GRAY if pixels.shape[2] == 4 else cv2.COLOR_RGB2GRAY
        pixels = cv2.cvtColor(pixels, conversion)
    if pixels.ndim != 2 or pixels.size == 0:
        logger.warning("Raster drawing must be a non-empty grayscale or RGB image.")
        return []
    return _convert_grayscale_array(pixels, target_dpi, cv2, target_bounds_mm)


def _convert_grayscale_array(image, target_dpi, cv2, target_bounds_mm=None):
    if not isinstance(target_dpi, (int, float)) or target_dpi <= 0:
        raise ValueError("target_dpi must be a positive number.")

    height, width = image.shape
    if height < 3 or width < 3:
        return []

    roi_top = int(height * 0.4)
    roi_bottom = int(height * 0.95)
    roi_right = int(width * 0.55)
    front_view_roi = image[roi_top:roi_bottom, 0:roi_right]
    if front_view_roi.size == 0:
        return []
    image = front_view_roi

    blurred = cv2.GaussianBlur(image, (5, 5), 0)
    binary = cv2.threshold(
        blurred, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU
    )[1]
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    contours, hierarchy = cv2.findContours(
        binary, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE
    )
    if hierarchy is None or not contours:
        return []

    pixel_to_mm = 25.4 / float(target_dpi)
    min_area = max(100.0, width * height * 0.00001)
    min_span = max(3.0, 1.5 / pixel_to_mm)
    candidates = []
    for index, contour in enumerate(contours):
        area = abs(cv2.contourArea(contour))
        if area < min_area or cv2.arcLength(contour, True) <= 0:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        if min(w, h) < min_span:
            continue
        if x <= 1 and y <= 1 and x + w >= width - 1 and y + h >= height - 1:
            continue
        candidates.append((index, contour, area, (x, y, w, h)))

    if not candidates:
        return []

    # Treat the largest closed contour as the profile hull. Retain substantial
    # contours contained by it (e.g. circular holes), while ignoring detached
    # dimension strings, leaders, and title-block geometry.
    hull_index, hull, hull_area, hull_box = max(candidates, key=lambda item: item[2])
    hx, hy, hw, hh = hull_box
    selected = []
    for candidate in candidates:
        index, contour, area, (x, y, w, h) = candidate
        if index == hull_index:
            selected.append((contour, area, (x, y, w, h)))
            continue
        if area >= hull_area:
            continue
        if x < hx or y < hy or x + w > hx + hw or y + h > hy + hh:
            continue
        if area / hull_area < 0.0005:
            continue
        selected.append((contour, area, (x, y, w, h)))

    # Prefer the outside contour for nested contours that are merely the two
    # pixel edges of one printed stroke.
    selected.sort(key=lambda item: item[1], reverse=True)
    filtered = []
    stroke_tolerance = max(2.0, 2.0 / pixel_to_mm)
    for contour, area, box in selected:
        x, y, w, h = box
        duplicate = False
        for _kept, _kept_area, kept_box in filtered:
            kx, ky, kw, kh = kept_box
            same_bounds = (
                abs(x - kx) <= stroke_tolerance
                and abs(y - ky) <= stroke_tolerance
                and abs((x + w) - (kx + kw)) <= stroke_tolerance
                and abs((y + h) - (ky + kh)) <= stroke_tolerance
            )
            if same_bounds:
                duplicate = True
                break
        if not duplicate:
            filtered.append((contour, area, box))

    origin_x, origin_y = hx, hy + hh
    hull_width_mm = hw * pixel_to_mm
    hull_height_mm = hh * pixel_to_mm
    scale_x = scale_y = 1.0
    if target_bounds_mm is not None:
        if (
            not isinstance(target_bounds_mm, (tuple, list))
            or len(target_bounds_mm) != 2
            or any(not isinstance(value, (int, float)) or value <= 0 for value in target_bounds_mm)
        ):
            raise ValueError("target_bounds_mm must be a pair of positive width and height values.")
        target_width, target_height = map(float, target_bounds_mm)
        scale_x = target_width / hull_width_mm
        scale_y = target_height / hull_height_mm
        if not np.isclose(scale_x, scale_y, rtol=0.02, atol=1e-6):
            raise ValueError(
                "Target bounds have a different aspect ratio from the raster profile; "
                "refusing to distort the geometry."
            )
        scale = (scale_x + scale_y) / 2
        scale_x = scale_y = scale

    def to_mm(x, y):
        return (
            round((float(x) - origin_x) * pixel_to_mm * scale_x * 2) / 2,
            round((origin_y - float(y)) * pixel_to_mm * scale_y * 2) / 2,
        )

    shapes = []
    for contour, _area, _box in filtered:
        perimeter = cv2.arcLength(contour, True)
        epsilon = 0.02 * perimeter
        polygon = cv2.approxPolyDP(contour, epsilon, True).reshape(-1, 2)
        if len(polygon) < 3:
            continue

        polygon_points = _snap_near_orthogonal(polygon)
        points = [to_mm(x, y) for x, y in polygon_points]
        contour_area = abs(cv2.contourArea(contour))
        perimeter = max(perimeter, 1e-9)
        circularity = 4.0 * np.pi * contour_area / (perimeter * perimeter)
        x, y, w, h = cv2.boundingRect(contour)
        aspect = min(w, h) / max(w, h)
        if circularity >= 0.68 and aspect >= 0.82:
            (cx, cy), radius = cv2.minEnclosingCircle(contour)
            rounded_cx, rounded_cy = to_mm(cx, cy)
            shapes.append({
                "type": "circle",
                "cx": rounded_cx,
                "cy": rounded_cy,
                "diameter": round(
                    2.0 * float(radius) * pixel_to_mm * (scale_x + scale_y) / 2 * 2
                ) / 2,
            })
        else:
            shapes.append({"type": "polygon", "points": points})

    return shapes


def _snap_near_orthogonal(points):
    """Snap only contour segments within five degrees of an axis."""
    snapped = np.asarray(points, dtype=np.float64).copy()
    if len(snapped) < 3:
        return snapped

    for index in range(len(snapped)):
        next_index = (index + 1) % len(snapped)
        dx = snapped[next_index, 0] - snapped[index, 0]
        dy = snapped[next_index, 1] - snapped[index, 1]
        if abs(dy) <= _ORTHOGONAL_TOLERANCE * abs(dx):
            shared_y = round((snapped[index, 1] + snapped[next_index, 1]) / 2)
            snapped[index, 1] = shared_y
            snapped[next_index, 1] = shared_y
        elif abs(dx) <= _ORTHOGONAL_TOLERANCE * abs(dy):
            shared_x = round((snapped[index, 0] + snapped[next_index, 0]) / 2)
            snapped[index, 0] = shared_x
            snapped[next_index, 0] = shared_x
    return snapped
