"""Local PDF content inspection for engineering-drawing diagnostics."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
import sys
from pathlib import Path
from typing import Any

from .pdf_raster_extract import extract_page_drawing_raster


class PdfInspectionError(ValueError):
    """A PDF could not be inspected or a requested diagnostic could not run."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _bbox(element: dict[str, Any]) -> dict[str, float] | None:
    keys = ("x0", "top", "x1", "bottom")
    if not all(key in element for key in keys):
        return None
    return {key: float(element[key]) for key in keys}


def _vector_summary(page: Any) -> list[dict[str, Any]]:
    vectors = []
    for kind, elements in (
        ("line", page.lines),
        ("rectangle", page.rects),
        ("curve", page.curves),
    ):
        for element in elements:
            item: dict[str, Any] = {
                "type": kind,
                "bbox_pt": _bbox(element),
            }
            if kind == "line":
                item["endpoints_pt"] = [
                    [
                        float(element["x0"]),
                        float(page.height - element["y0"]),
                    ],
                    [
                        float(element["x1"]),
                        float(page.height - element["y1"]),
                    ],
                ]
            elif kind == "rectangle":
                item["width_pt"] = float(element["x1"] - element["x0"])
                item["height_pt"] = float(element["bottom"] - element["top"])
            elif "path" in element:
                item["path_segment_count"] = len(element["path"])
            vectors.append(item)
    return vectors


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


def inspect_pdf(
    pdf_path: str | os.PathLike[str],
    *,
    render_dpi: int = 300,
    render_dir: str | os.PathLike[str] | None = None,
    classify: bool = False,
    analyze_raster: bool = False,
    analysis_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Inspect PDF pages without attempting to infer or create a 3D model.

    PDF coordinates are reported in points with an origin at the page's
    top-left, matching pdfplumber. Rendering is opt-in through ``render_dir``.
    """
    path = Path(pdf_path)
    if not path.is_file():
        raise PdfInspectionError("PDF_READ_ERROR", f"PDF file does not exist: {path}")
    if path.suffix.lower() != ".pdf":
        raise PdfInspectionError("PDF_READ_ERROR", "Input must be a PDF file.")
    if not isinstance(render_dpi, int) or isinstance(render_dpi, bool) or render_dpi <= 0:
        raise ValueError("render_dpi must be a positive integer.")

    try:
        import pdfplumber
    except ImportError as exc:
        raise PdfInspectionError(
            "PDF_READ_ERROR",
            "pdfplumber is required for PDF inspection; install project requirements.",
        ) from exc

    pages = []
    try:
        with pdfplumber.open(path) as document:
            if not document.pages:
                raise PdfInspectionError("PDF_READ_ERROR", "The PDF contains no pages.")
            for page_index, page in enumerate(document.pages):
                vectors = _vector_summary(page)
                images = [
                    {
                        "bbox_pt": _bbox(image),
                        "pixel_width": int(image.get("width", 0)),
                        "pixel_height": int(image.get("height", 0)),
                    }
                    for image in page.images
                ]
                text = [
                    {
                        "text": word["text"],
                        "bbox_pt": {
                            key: float(word[key])
                            for key in ("x0", "top", "x1", "bottom")
                        },
                    }
                    for word in page.extract_words()
                ]
                content_type = _content_type(bool(vectors), bool(images), bool(text))
                route, reason = _route(content_type, len(vectors), len(images))
                page_report = {
                    "page_number": page_index + 1,
                    "width_pt": float(page.width),
                    "height_pt": float(page.height),
                    "rotation_degrees": int(getattr(page, "rotation", 0) or 0),
                    "content_type": content_type,
                    "native_text": text,
                    "vector_entities": vectors,
                    "embedded_images": images,
                    "processing_route": route,
                    "route_reason": reason,
                }
                if classify:
                    page_report["classified_entities"] = _classify_page_entities(page_report)
                if analyze_raster and images:
                    page_report["raster_analysis"] = _analyze_page_raster(
                        pdf_path=path,
                        page=page,
                        page_number=page_index + 1,
                        dpi=render_dpi,
                        output_dir=analysis_dir,
                    )
                pages.append(page_report)
    except PdfInspectionError:
        raise
    except Exception as exc:
        raise PdfInspectionError(
            "PDF_READ_ERROR", f"Could not inspect PDF '{path.name}': {exc}"
        ) from exc

    report: dict[str, Any] = {
        "status": "ok",
        "source_file": path.name,
        "coordinate_system": {
            "unit": "PDF points",
            "origin": "page top-left",
            "points_per_inch": 72,
        },
        "page_count": len(pages),
        "pages": pages,
        "summary": {
            "vector_entity_count": sum(len(page["vector_entities"]) for page in pages),
            "embedded_image_count": sum(len(page["embedded_images"]) for page in pages),
            "native_word_count": sum(len(page["native_text"]) for page in pages),
            "content_types": sorted({page["content_type"] for page in pages}),
            "processing_routes": sorted({page["processing_route"] for page in pages}),
        },
    }
    if classify:
        report["summary"]["classified_role_counts"] = _summarize_roles(pages)

    if render_dir is not None:
        _render_pages(path, Path(render_dir), render_dpi, report["pages"])
        report["rendering"] = {
            "dpi": render_dpi,
            "directory": str(Path(render_dir)),
        }
    else:
        report["rendering"] = {"status": "not_requested"}
    return report


def _summarize_roles(pages: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for page in pages:
        for entity in page.get("classified_entities", []):
            role = entity.get("role")
            if isinstance(role, str):
                counts[role] += 1
    return dict(sorted(counts.items()))


def _classify_page_entities(page: dict[str, Any]) -> list[dict[str, Any]]:
    entities = []
    for vector in page.get("vector_entities", []):
        kind = vector.get("type")
        if kind in {"line", "curve"}:
            role = "geometry"
            evidence = ["vector-stroke"]
        elif kind == "rectangle":
            bbox = vector.get("bbox_pt") or {}
            width = float(vector.get("width_pt") or 0.0)
            height = float(vector.get("height_pt") or 0.0)
            near_page_border = (
                bbox.get("x0", 1e9) <= 3.0
                and bbox.get("top", 1e9) <= 3.0
                and abs(float(page.get("width_pt", 0.0)) - float(bbox.get("x1", 0.0))) <= 3.0
                and abs(float(page.get("height_pt", 0.0)) - float(bbox.get("bottom", 0.0))) <= 3.0
            )
            if near_page_border and width > 0 and height > 0:
                role = "annotation"
                evidence = ["border-like-rectangle"]
            else:
                role = "geometry"
                evidence = ["closed-vector-rectangle"]
        else:
            role = "unknown"
            evidence = ["unsupported-vector-entity"]
        entities.append({
            "kind": kind,
            "role": role,
            "geometry": vector,
            "evidence": evidence,
        })

    for text in page.get("native_text", []):
        token = str(text.get("text") or "")
        if any(marker in token.upper() for marker in ("Ø", "⌀", "R", "M", "°")) or any(ch.isdigit() for ch in token):
            role = "dimension"
            evidence = ["dimension-text-token"]
        else:
            role = "annotation"
            evidence = ["native-text-token"]
        entities.append({
            "kind": "text",
            "role": role,
            "geometry": text,
            "evidence": evidence,
        })

    for image in page.get("embedded_images", []):
        entities.append({
            "kind": "image",
            "role": "annotation",
            "geometry": image,
            "evidence": ["embedded-raster-content"],
        })
    return entities


def _analyze_page_raster(
    *,
    pdf_path: Path,
    page: Any,
    page_number: int,
    dpi: int,
    output_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    try:
        from .raster_drawing_analyzer import analyze_raster_drawing
    except ImportError as exc:
        return {
            "status": "unavailable",
            "note": f"Raster analysis unavailable: {exc}",
        }

    image, extraction = extract_page_drawing_raster(pdf_path, page, page_number - 1, dpi=dpi)
    if image is None:
        return {
            "status": "error",
            "note": "No raster content was extractable for deterministic analysis.",
            "extraction": extraction,
        }

    overlay_path = None
    if output_dir is not None:
        overlay_path = Path(output_dir) / f"page-{page_number:03d}-raster-overlay.png"
    return analyze_raster_drawing(image, output_png=overlay_path, extraction=extraction)


def _render_pages(
    pdf_path: Path,
    render_dir: Path,
    dpi: int,
    pages: list[dict[str, Any]],
) -> None:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise PdfInspectionError(
            "PDF_READ_ERROR",
            "pypdfium2 is required to render diagnostic page images.",
        ) from exc

    render_dir.mkdir(parents=True, exist_ok=True)
    document = None
    try:
        document = pdfium.PdfDocument(str(pdf_path))
        for page_index, page_report in enumerate(pages):
            pdf_page = document[page_index]
            bitmap = None
            image = None
            try:
                bitmap = pdf_page.render(scale=dpi / 72.0)
                image = bitmap.to_pil()
                filename = f"page-{page_index + 1:03d}.png"
                image.save(render_dir / filename, format="PNG")
                page_report["render"] = {
                    "file": filename,
                    "width_px": int(image.width),
                    "height_px": int(image.height),
                }
            finally:
                if image is not None:
                    image.close()
                if bitmap is not None:
                    bitmap.close()
                pdf_page.close()
    except Exception as exc:
        raise PdfInspectionError(
            "PDF_READ_ERROR", f"Could not render PDF pages: {exc}"
        ) from exc
    finally:
        if document is not None:
            document.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect PDF drawing content locally.")
    parser.add_argument("pdf", help="PDF file to inspect")
    parser.add_argument("--output", help="Write JSON report to this file (default: stdout)")
    parser.add_argument(
        "--classify",
        action="store_true",
        help="Classify detected native entities into deterministic role candidates.",
    )
    parser.add_argument(
        "--analyze-raster",
        action="store_true",
        help="Run deterministic raster analysis for embedded drawing images.",
    )
    parser.add_argument(
        "--render-dir",
        help="Optionally save rendered page PNGs to this directory",
    )
    parser.add_argument(
        "--dpi", type=int, default=300, help="Page-render resolution (default: 300)"
    )
    args = parser.parse_args(argv)
    analysis_dir = None
    if args.analyze_raster:
        if args.render_dir:
            analysis_dir = args.render_dir
        elif args.output:
            analysis_dir = str(Path(args.output).resolve().parent)
        else:
            analysis_dir = str(Path.cwd())
    try:
        report = inspect_pdf(
            args.pdf,
            render_dpi=args.dpi,
            render_dir=args.render_dir,
            classify=args.classify,
            analyze_raster=args.analyze_raster,
            analysis_dir=analysis_dir,
        )
    except (PdfInspectionError, ValueError) as exc:
        print(f"{getattr(exc, 'code', 'INVALID_ARGUMENT')}: {exc}", file=sys.stderr)
        return 2

    serialized = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(serialized + "\n", encoding="utf-8")
    else:
        print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
