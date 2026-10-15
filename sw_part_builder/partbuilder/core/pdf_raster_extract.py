"""Deterministic PDF raster extraction helpers."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image


def select_likely_drawing_image(
    images: list[dict[str, Any]],
    page_width_pt: float,
    page_height_pt: float,
) -> tuple[int, dict[str, Any]] | None:
    """Select the most likely engineering-drawing raster image on a page."""
    if not images:
        return None
    page_area = max(float(page_width_pt) * float(page_height_pt), 1.0)

    def score(item: tuple[int, dict[str, Any]]) -> float:
        index, image = item
        width_pt = max(float(image.get("x1", 0.0)) - float(image.get("x0", 0.0)), 0.0)
        height_pt = max(float(image.get("bottom", 0.0)) - float(image.get("top", 0.0)), 0.0)
        area_score = (width_pt * height_pt) / page_area
        src_w, src_h = image.get("srcsize") or (0, 0)
        src_score = min(float(src_w) * float(src_h), 25_000_000.0) / 25_000_000.0
        return area_score * 0.8 + src_score * 0.2 - index * 1e-6

    return max(enumerate(images), key=score, default=None)


def _as_grayscale(pixels: np.ndarray) -> np.ndarray:
    if pixels.ndim == 2:
        return pixels.astype(np.uint8, copy=False)
    if pixels.ndim == 3:
        if pixels.shape[2] == 4:
            rgb = pixels[:, :, :3].astype(np.float32)
            alpha = (pixels[:, :, 3:4].astype(np.float32) / 255.0)
            white = np.full_like(rgb, 255.0)
            rgb = rgb * alpha + white * (1.0 - alpha)
        else:
            rgb = pixels[:, :, :3].astype(np.float32)
        gray = 0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]
        return np.clip(gray, 0.0, 255.0).astype(np.uint8)
    return np.zeros((0, 0), dtype=np.uint8)


def _stream_to_array(image_dict: dict[str, Any]) -> np.ndarray | None:
    stream = image_dict.get("stream")
    if stream is None or not hasattr(stream, "get_data"):
        return None
    try:
        decoded = stream.get_data()
    except Exception:
        return None
    if not decoded:
        return None

    src_width, src_height = image_dict.get("srcsize") or (0, 0)
    width = int(src_width) if src_width else int(getattr(stream, "attrs", {}).get("Width", 0))
    height = int(src_height) if src_height else int(getattr(stream, "attrs", {}).get("Height", 0))
    if width <= 0 or height <= 0:
        return None

    colorspace = image_dict.get("colorspace") or []
    colorspace_text = ",".join(str(token) for token in colorspace).lower()
    if "devicecmyk" in colorspace_text:
        channels = 4
        mode = "CMYK"
    elif "devicergb" in colorspace_text:
        channels = 3
        mode = "RGB"
    else:
        channels = 1
        mode = "L"
    expected = width * height * channels
    bits = int(image_dict.get("bits") or 8)
    if bits == 8 and len(decoded) == expected:
        array = np.frombuffer(decoded, dtype=np.uint8).reshape((height, width, channels))
        if channels == 1:
            return array.reshape((height, width))
        if mode == "CMYK":
            pil = Image.fromarray(array, mode=mode).convert("RGB")
            try:
                return np.asarray(pil)
            finally:
                pil.close()
        return array
    try:
        pil = Image.open(BytesIO(decoded))
        try:
            return np.asarray(pil.convert("RGBA"))
        finally:
            pil.close()
    except Exception:
        return None


def _render_page_grayscale(
    pdf_path: Path,
    page_index: int,
    dpi: int,
) -> np.ndarray | None:
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return None
    document = None
    try:
        document = pdfium.PdfDocument(str(pdf_path))
        pdf_page = document[page_index]
        bitmap = None
        pil_image = None
        try:
            bitmap = pdf_page.render(scale=dpi / 72.0)
            pil_image = bitmap.to_pil()
            return np.asarray(pil_image.convert("L"))
        finally:
            if pil_image is not None:
                pil_image.close()
            if bitmap is not None:
                bitmap.close()
            pdf_page.close()
    except Exception:
        return None
    finally:
        if document is not None:
            document.close()


def extract_page_drawing_raster(
    pdf_path: str | Path,
    page: Any,
    page_index: int,
    *,
    dpi: int = 300,
) -> tuple[np.ndarray | None, dict[str, Any]]:
    """Extract a likely drawing raster from a PDF page.

    Preferred order:
    1. Decode the selected embedded image stream directly.
    2. Fallback to rendering the page and cropping to selected image bounds.
    3. Fallback to rendering the full page.
    """
    path = Path(pdf_path)
    selection = select_likely_drawing_image(page.images, page.width, page.height)
    if selection is not None:
        image_index, selected = selection
        raw_array = _stream_to_array(selected)
        if raw_array is not None and raw_array.size:
            return _as_grayscale(raw_array), {
                "extraction_method": "embedded-image-stream",
                "fallback_used": False,
                "selected_image_index": image_index,
                "selected_bbox_pt": {
                    "x0": float(selected["x0"]),
                    "top": float(selected["top"]),
                    "x1": float(selected["x1"]),
                    "bottom": float(selected["bottom"]),
                },
            }

        rendered = _render_page_grayscale(path, page_index, dpi)
        if rendered is not None and rendered.size:
            sx = rendered.shape[1] / max(float(page.width), 1.0)
            sy = rendered.shape[0] / max(float(page.height), 1.0)
            left = int(max(round(float(selected["x0"]) * sx), 0))
            top = int(max(round(float(selected["top"]) * sy), 0))
            right = int(min(round(float(selected["x1"]) * sx), rendered.shape[1]))
            bottom = int(min(round(float(selected["bottom"]) * sy), rendered.shape[0]))
            if right - left >= 4 and bottom - top >= 4:
                return rendered[top:bottom, left:right].copy(), {
                    "extraction_method": "embedded-image-render-crop",
                    "fallback_used": True,
                    "fallback_reason": "stream-decode-failed",
                    "selected_image_index": image_index,
                    "selected_bbox_pt": {
                        "x0": float(selected["x0"]),
                        "top": float(selected["top"]),
                        "x1": float(selected["x1"]),
                        "bottom": float(selected["bottom"]),
                    },
                }

    full_page = _render_page_grayscale(path, page_index, dpi)
    if full_page is not None and full_page.size:
        return full_page, {
            "extraction_method": "page-render-fallback",
            "fallback_used": True,
            "fallback_reason": "no-decodable-embedded-image",
        }
    return None, {
        "extraction_method": "unavailable",
        "fallback_used": True,
        "fallback_reason": "no-raster-data",
    }
