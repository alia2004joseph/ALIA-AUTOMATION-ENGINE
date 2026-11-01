"""Optional local Tesseract OCR adapter for raster drawing text candidates."""
from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
import shutil
from typing import Any


def _find_tesseract() -> Path | None:
    configured = os.environ.get("ALIA_TESSERACT_CMD")
    if configured:
        candidate = Path(configured).expanduser()
        return candidate if candidate.is_file() else None

    discovered = shutil.which("tesseract")
    if discovered:
        return Path(discovered)
    if os.name == "nt":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        candidates = [
            Path(program_files) / "Tesseract-OCR" / "tesseract.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            / "Tesseract-OCR" / "tesseract.exe",
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Programs" / "Tesseract-OCR" / "tesseract.exe",
        ]
        return next((candidate for candidate in candidates if candidate.is_file()), None)
    return None


@lru_cache(maxsize=1)
def _engine_status() -> dict[str, Any]:
    try:
        import pytesseract
    except ImportError as exc:
        return {
            "status": "dependency_missing",
            "engine": "Tesseract",
            "error": f"pytesseract is not installed: {exc}",
        }

    executable = _find_tesseract()
    if executable is None:
        return {
            "status": "engine_missing",
            "engine": "Tesseract",
            "wrapper_version": pytesseract.__version__,
            "error": (
                "Tesseract executable was not found. Add it to PATH or set "
                "ALIA_TESSERACT_CMD to its full path."
            ),
        }

    pytesseract.pytesseract.tesseract_cmd = str(executable)
    try:
        version = str(pytesseract.get_tesseract_version()).strip()
    except (pytesseract.TesseractNotFoundError, OSError) as exc:
        return {
            "status": "engine_error",
            "engine": "Tesseract",
            "executable": str(executable),
            "wrapper_version": pytesseract.__version__,
            "error": str(exc),
        }

    return {
        "status": "available",
        "engine": "Tesseract",
        "executable": str(executable),
        "engine_version": version,
        "wrapper_version": pytesseract.__version__,
        "language": "eng",
    }


def get_offline_ocr_status() -> dict[str, Any]:
    """Return whether the local OCR engine can run, without contacting a service."""
    return dict(_engine_status())


def recognize_image_text(image: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Recognize printed words and pixel boxes; results remain unverified candidates."""
    try:
        import numpy as np
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        return [], {
            "status": "dependency_missing",
            "engine": "Tesseract",
            "error": str(exc),
        }

    pixels = np.asarray(image)
    if pixels.ndim != 2 or pixels.size == 0:
        raise ValueError("Offline OCR requires a non-empty grayscale image.")

    status = get_offline_ocr_status()
    if status["status"] != "available":
        return [], status

    pil_image = Image.fromarray(pixels.astype(np.uint8, copy=False))
    try:
        data = pytesseract.image_to_data(
            pil_image,
            lang="eng",
            config="--psm 11",
            output_type=pytesseract.Output.DICT,
            timeout=60,
        )
    except pytesseract.TesseractNotFoundError as exc:
        return [], {
            **status,
            "status": "engine_missing",
            "error": str(exc),
        }
    except pytesseract.TesseractError as exc:
        return [], {
            **status,
            "status": "recognition_error",
            "error": str(exc),
        }
    except RuntimeError as exc:
        return [], {
            **status,
            "status": "timeout",
            "error": str(exc),
        }
    finally:
        pil_image.close()

    words = []
    for index, value in enumerate(data.get("text", [])):
        text = str(value).strip()
        if not text:
            continue
        try:
            engine_score = float(data["conf"][index])
        except (KeyError, TypeError, ValueError):
            engine_score = -1.0
        if engine_score < 0:
            continue
        left = int(data["left"][index])
        top = int(data["top"][index])
        width = int(data["width"][index])
        height = int(data["height"][index])
        if width <= 0 or height <= 0:
            continue
        words.append({
            "text": text,
            "bbox_px": {
                "x0": left,
                "top": top,
                "x1": left + width,
                "bottom": top + height,
            },
            "recognition_method": "offline_tesseract",
            "recognition_confidence": None,
            "engine_score_raw": engine_score,
            "confidence_status": "uncalibrated",
            "information_status": "directly_observed",
            "review_required": True,
        })

    result_status = {
        **status,
        "status": "completed",
        "word_count": len(words),
        "coordinate_system": {
            "unit": "pixels",
            "origin": "image top-left",
        },
        "limitations": [
            "Tesseract engine scores are not calibrated engineering-dimension confidence.",
            "OCR text is a candidate only and requires human verification.",
            "Graphical symbol recognition is not provided by this OCR pass.",
        ],
    }
    return words, result_status
