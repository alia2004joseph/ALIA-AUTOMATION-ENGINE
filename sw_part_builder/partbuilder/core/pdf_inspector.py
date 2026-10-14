"""Local PDF content inspection for engineering-drawing diagnostics."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


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
    include_classified_entities: bool = False,
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
        from ..drawing.pdf_drawing import PdfDrawingParseError, parse_pdf_drawing

        drawing = parse_pdf_drawing(path)
    except PdfDrawingParseError as exc:
        raise PdfInspectionError(exc.code, str(exc)) from exc
    report: dict[str, Any] = drawing.to_report(include_entities=include_classified_entities)

    if render_dir is not None:
        _render_pages(path, Path(render_dir), render_dpi, report["pages"])
        report["rendering"] = {
            "dpi": render_dpi,
            "directory": str(Path(render_dir)),
        }
    else:
        report["rendering"] = {"status": "not_requested"}
    return report


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
        "--render-dir",
        help="Optionally save rendered page PNGs to this directory",
    )
    parser.add_argument(
        "--dpi", type=int, default=300, help="Page-render resolution (default: 300)"
    )
    parser.add_argument(
        "--classify",
        action="store_true",
        help="Include deterministic classified entities with geometry/dimension roles.",
    )
    args = parser.parse_args(argv)
    try:
        report = inspect_pdf(
            args.pdf,
            render_dpi=args.dpi,
            render_dir=args.render_dir,
            include_classified_entities=args.classify,
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
