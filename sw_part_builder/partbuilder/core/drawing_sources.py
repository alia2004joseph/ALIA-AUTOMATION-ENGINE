"""Common local inspection entry point for PDF, raster-image, and DXF sources."""
from __future__ import annotations

from pathlib import Path
from typing import Any


class DrawingSourceError(ValueError):
    """The requested drawing source could not be inspected."""


def _inspect_raster(
    path: Path,
    output_dir: Path | None,
    run_ocr: bool,
) -> dict[str, Any]:
    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        raise DrawingSourceError(
            "Raster inspection requires Pillow, NumPy, and OpenCV from requirements.txt."
        ) from exc

    try:
        with Image.open(path) as source:
            source.load()
            source_dpi = source.info.get("dpi")
            image = np.asarray(source.convert("L"))
    except (OSError, ValueError) as exc:
        raise DrawingSourceError(f"Could not decode drawing image '{path.name}': {exc}") from exc

    analysis_dir = output_dir or path.with_name(f"{path.stem}-drawing-analysis")
    analysis_dir.mkdir(parents=True, exist_ok=True)
    page_png = analysis_dir / "page-001.png"
    overlay_png = analysis_dir / "page-001-raster-overlay.png"
    orthographic_png = analysis_dir / "page-001-orthographic-views.png"
    drawing_layout_png = analysis_dir / "page-001-drawing-view-layout.png"
    from .raster_drawing_analyzer import analyze_raster_drawing

    raster_report = analyze_raster_drawing(
        image,
        output_png=overlay_png,
        orthographic_views_png=orthographic_png,
        drawing_layout_png=drawing_layout_png,
        preserve_full_frame=True,
    )
    from .offline_ocr import get_offline_ocr_status, recognize_image_text

    if run_ocr:
        ocr_words, ocr_status = recognize_image_text(image)
    else:
        ocr_words = []
        ocr_status = {
            **get_offline_ocr_status(),
            "status": "not_requested",
        }
    with Image.fromarray(image) as rendered:
        rendered.save(page_png, format="PNG")

    return {
        "status": "ok",
        "source_file": path.name,
        "source_document": {
            "file_name": path.name,
            "path": str(path),
            "format": path.suffix.lower().lstrip("."),
            "input_type": "raster_image",
            "metadata_dpi": source_dpi,
            "coordinate_system": {
                "unit": "pixels",
                "origin": "top-left",
            },
        },
        "input_type": "raster_image",
        "coordinate_system": {"unit": "pixels", "origin": "top-left"},
        "page_count": 1,
        "pages": [{
            "page_number": 1,
            "width_px": image.shape[1],
            "height_px": image.shape[0],
            "coordinate_system": {"unit": "pixels", "origin": "top-left"},
            "content_type": "raster",
            "native_text": [],
            "ocr_text": ocr_words,
            "vector_entities": [],
            "render": {
                "file": page_png.name,
                "width_px": image.shape[1],
                "height_px": image.shape[0],
            },
            "raster_analysis": raster_report,
            "units_evidence": {
                "candidate": None,
                "status": "unknown",
                "evidence": [],
            },
            "scale_evidence": {
                "candidate": None,
                "status": "metadata_candidate" if source_dpi else "unknown",
                "evidence": ([{
                    "source": "image metadata",
                    "dpi": list(source_dpi) if isinstance(source_dpi, tuple) else source_dpi,
                    "limitation": "DPI metadata does not establish original 1:1 drawing scale.",
                }] if source_dpi else []),
            },
            "offline_ocr": ocr_status,
        }],
        "summary": {
            "vector_entity_count": 0,
            "embedded_image_count": 1,
            "native_word_count": 0,
            "ocr_word_count": len(ocr_words),
            "content_types": ["raster"],
            "processing_routes": ["raster-preprocessing"],
        },
        "rendering": {
            "status": "source_resolution_preserved",
            "directory": str(analysis_dir),
        },
    }


def _dxf_entity_record(entity: Any) -> dict[str, Any]:
    kind = entity.dxftype()
    record: dict[str, Any] = {
        "type": kind,
        "layer": str(entity.dxf.get("layer", "0")),
        "handle": str(entity.dxf.get("handle", "")),
    }

    def point(value: Any) -> list[float]:
        return [float(value.x), float(value.y), float(value.z)]

    if kind == "LINE":
        record["start"] = point(entity.dxf.start)
        record["end"] = point(entity.dxf.end)
    elif kind in {"CIRCLE", "ARC"}:
        record["center"] = point(entity.dxf.center)
        record["radius"] = float(entity.dxf.radius)
        if kind == "ARC":
            record["start_angle_degrees"] = float(entity.dxf.start_angle)
            record["end_angle_degrees"] = float(entity.dxf.end_angle)
    elif kind == "LWPOLYLINE":
        record["vertices"] = [
            [float(vertex[0]), float(vertex[1])]
            for vertex in entity.get_points("xy")
        ]
        record["closed"] = bool(entity.closed)
    elif kind == "POLYLINE":
        record["vertices"] = [
            point(vertex.dxf.location) for vertex in entity.vertices
        ]
        record["closed"] = bool(entity.is_closed)
    elif kind in {"TEXT", "MTEXT", "DIMENSION"}:
        record["text"] = str(entity.dxf.get("text", ""))
        insertion = entity.dxf.get("insert")
        if insertion is not None:
            record["insertion"] = point(insertion)
    return record


def _inspect_dxf(path: Path, output_dir: Path | None) -> dict[str, Any]:
    try:
        import ezdxf
        from ezdxf import units
    except ImportError as exc:
        raise DrawingSourceError(
            "DXF inspection requires ezdxf from requirements.txt."
        ) from exc

    try:
        document = ezdxf.readfile(str(path))
        modelspace = document.modelspace()
        entities = [_dxf_entity_record(entity) for entity in modelspace]
    except (OSError, ValueError, ezdxf.DXFError) as exc:
        raise DrawingSourceError(f"Could not inspect DXF '{path.name}': {exc}") from exc

    unit_code = int(document.units or 0)
    try:
        unit_name = units.unit_name(unit_code) if unit_code else None
    except (ValueError, KeyError, TypeError):
        unit_name = None
    from .validate import parse_universal_dxf

    profile_result = parse_universal_dxf(str(path))
    analysis_dir = output_dir or path.parent
    return {
        "status": "ok",
        "source_file": path.name,
        "source_document": {
            "file_name": path.name,
            "path": str(path),
            "format": "dxf",
            "input_type": "vector_dxf",
            "units": unit_name,
            "coordinate_system": {
                "unit": unit_name or "unspecified DXF drawing units",
                "origin": "DXF model-space coordinates",
            },
        },
        "input_type": "vector_dxf",
        "coordinate_system": {
            "unit": unit_name or "unspecified DXF drawing units",
            "origin": "DXF model-space coordinates",
        },
        "page_count": 1,
        "pages": [{
            "page_number": 1,
            "content_type": "vector",
            "native_text": [],
            "vector_entities": entities,
            "dxf_profile_parse": profile_result,
            "coordinate_system": {
                "unit": unit_name or "unspecified DXF drawing units",
                "origin": "DXF model-space coordinates",
            },
            "units_evidence": {
                "candidate": unit_name,
                "status": "declared" if unit_name else "unknown",
                "evidence": ([{
                    "source": "$INSUNITS",
                    "value": unit_code,
                    "recognition_method": "DXF header",
                }] if unit_name else []),
            },
            "scale_evidence": {
                "candidate": None,
                "status": "unknown",
                "evidence": [],
            },
        }],
        "summary": {
            "vector_entity_count": len(entities),
            "embedded_image_count": 0,
            "native_word_count": 0,
            "content_types": ["vector"],
            "processing_routes": ["dxf-vector-inspection"],
        },
        "rendering": {"status": "not_available_for_dxf"},
        "output_directory": str(analysis_dir),
    }


def inspect_drawing_source(
    source_path: str | Path,
    *,
    dpi: int = 300,
    output_dir: str | Path | None = None,
    run_ocr: bool = False,
) -> dict[str, Any]:
    """Inspect one supported source while preserving source-specific evidence.

    PDF keeps its existing text/vector/raster inspection path. Image and DXF
    inputs are normalized into the same report envelope without inventing
    pixel-to-length scale or projecting DXF geometry into PDF coordinates.
    """
    path = Path(source_path)
    if not path.is_file():
        raise DrawingSourceError(f"Drawing source does not exist: {path}")
    if not isinstance(dpi, int) or isinstance(dpi, bool) or dpi <= 0:
        raise ValueError("dpi must be a positive integer.")
    analysis_dir = Path(output_dir) if output_dir is not None else None
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from .pdf_inspector import inspect_pdf

        return inspect_pdf(
            path,
            render_dpi=dpi,
            render_dir=analysis_dir,
            classify=True,
            analyze_raster=True,
            run_ocr=run_ocr,
            analysis_dir=analysis_dir,
            preserve_overlay_frame=True,
        )
    if suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}:
        return _inspect_raster(path, analysis_dir, run_ocr)
    if suffix == ".dxf":
        report = _inspect_dxf(path, analysis_dir)
        report["offline_ocr"] = {"status": "not_applicable_vector_source"}
        return report
    raise DrawingSourceError(
        "Unsupported drawing format. Choose PDF, DXF, PNG, JPG, JPEG, TIFF, or BMP."
    )
