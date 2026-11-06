"""Prepare drawing images and OCR-masked copies for view-detection labeling.

This script deliberately does not create view labels. It creates clean image
copies, OCR reports, and a drawing-level split manifest for later annotation.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from partbuilder.core.offline_ocr import recognize_image_text


def _render_pdf(path: Path, dpi: int) -> list[tuple[str, np.ndarray]]:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise RuntimeError(
            "PDF input requires pypdfium2. Install requirements.txt first."
        ) from exc

    document = pdfium.PdfDocument(str(path))
    pages: list[tuple[str, np.ndarray]] = []
    try:
        for index in range(len(document)):
            page = document[index]
            bitmap = None
            pil_image = None
            try:
                bitmap = page.render(scale=dpi / 72.0)
                pil_image = bitmap.to_pil()
                pages.append(
                    (
                        f"{path.stem}-page-{index + 1:03d}",
                        np.asarray(pil_image.convert("RGB")),
                    )
                )
            finally:
                if pil_image is not None:
                    pil_image.close()
                if bitmap is not None:
                    bitmap.close()
                page.close()
    finally:
        document.close()
    return pages


def _load_source(path: Path, dpi: int) -> list[tuple[str, np.ndarray]]:
    if path.suffix.lower() == ".pdf":
        return _render_pdf(path, dpi)
    with Image.open(path) as image:
        image.load()
        return [(path.stem, np.asarray(image.convert("RGB")))]


def _mask_ocr_text(image: np.ndarray, words: list[dict[str, Any]]) -> np.ndarray:
    masked = Image.fromarray(image.copy())
    draw = ImageDraw.Draw(masked)
    for word in words:
        box = word.get("bbox_px", {})
        try:
            left = max(0, int(box["x0"]) - 3)
            top = max(0, int(box["top"]) - 3)
            right = min(masked.width, int(box["x1"]) + 3)
            bottom = min(masked.height, int(box["bottom"]) + 3)
        except (KeyError, TypeError, ValueError):
            continue
        if right > left and bottom > top:
            draw.rectangle((left, top, right, bottom), fill=(255, 255, 255))
    return np.asarray(masked)


def _iter_inputs(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    if input_path.is_dir():
        supported = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
        return sorted(
            path for path in input_path.rglob("*")
            if path.is_file() and path.suffix.lower() in supported
        )
    raise FileNotFoundError(f"Input path does not exist: {input_path}")


def _split_sources(
    source_ids: list[str],
    seed: int,
    validation_ratio: float,
    test_ratio: float,
) -> dict[str, str]:
    if validation_ratio < 0 or test_ratio < 0 or validation_ratio + test_ratio >= 1:
        raise ValueError("Validation and test ratios must be non-negative and sum to less than 1.")
    shuffled = list(dict.fromkeys(source_ids))
    random.Random(seed).shuffle(shuffled)
    total = len(shuffled)
    test_count = round(total * test_ratio)
    validation_count = round(total * validation_ratio)
    assignments: dict[str, str] = {}
    for index, source_id in enumerate(shuffled):
        if index < test_count:
            split = "test"
        elif index < test_count + validation_count:
            split = "val"
        else:
            split = "train"
        assignments[source_id] = split
    return assignments


def prepare_dataset(
    input_path: Path,
    output_dir: Path,
    dpi: int,
    seed: int,
    validation_ratio: float,
    test_ratio: float,
) -> dict[str, Any]:
    sources = _iter_inputs(input_path)
    if not sources:
        raise ValueError(f"No supported PDF or image files found under: {input_path}")

    raw_dir = output_dir / "images" / "raw"
    masked_dir = output_dir / "images" / "text-masked"
    ocr_dir = output_dir / "ocr"
    for directory in (raw_dir, masked_dir, ocr_dir):
        directory.mkdir(parents=True, exist_ok=True)

    source_ids = [path.stem for path in sources]
    split_by_source = _split_sources(
        source_ids, seed, validation_ratio, test_ratio
    )
    samples: list[dict[str, Any]] = []
    for source_path in sources:
        for sample_id, image in _load_source(source_path, dpi):
            gray = np.asarray(Image.fromarray(image).convert("L"))
            words, ocr_status = recognize_image_text(gray)
            masked = _mask_ocr_text(image, words)
            raw_path = raw_dir / f"{sample_id}.png"
            masked_path = masked_dir / f"{sample_id}.png"
            ocr_path = ocr_dir / f"{sample_id}.json"
            Image.fromarray(image).save(raw_path, format="PNG")
            Image.fromarray(masked).save(masked_path, format="PNG")
            ocr_path.write_text(
                json.dumps(
                    {
                        "source_file": str(source_path),
                        "sample_id": sample_id,
                        "ocr_status": ocr_status,
                        "words": words,
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            samples.append(
                {
                    "sample_id": sample_id,
                    "source_id": source_path.stem,
                    "source_file": str(source_path),
                    "split": split_by_source[source_path.stem],
                    "width_px": int(image.shape[1]),
                    "height_px": int(image.shape[0]),
                    "raw_image": str(raw_path.relative_to(output_dir)),
                    "text_masked_image": str(masked_path.relative_to(output_dir)),
                    "ocr_report": str(ocr_path.relative_to(output_dir)),
                    "annotation_status": "not_annotated",
                }
            )

    manifest = {
        "schema_version": 1,
        "task": "drawing_view_detection",
        "split_policy": "All pages from one source drawing share one split.",
        "text_masking": "OCR-detected word boxes are painted white in text-masked images.",
        "classes_to_annotate": ["orthographic", "section", "detail", "isometric"],
        "seed": seed,
        "render_dpi": dpi,
        "samples": samples,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create raw and OCR-text-masked images for view annotation."
    )
    parser.add_argument("input", type=Path, help="PDF/image file or directory")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/training/view_detection"),
        help="Dataset output directory",
    )
    parser.add_argument("--dpi", type=int, default=300, help="PDF render DPI")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--val-ratio", type=float, default=0.15)
    parser.add_argument("--test-ratio", type=float, default=0.15)
    args = parser.parse_args(argv)
    try:
        manifest = prepare_dataset(
            args.input,
            args.output,
            args.dpi,
            args.seed,
            args.val_ratio,
            args.test_ratio,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Prepared {len(manifest['samples'])} image sample(s).")
    print(f"Manifest: {args.output / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
