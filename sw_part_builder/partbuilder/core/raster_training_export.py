"""Persist raster-analysis images for later view-detector training."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any


def _safe_stem(value: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return stem or "drawing"


def _copy_if_file(source: str | Path | None, destination: Path) -> str | None:
    if not source:
        return None
    source_path = Path(source)
    if not source_path.is_file():
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, destination)
    return str(destination)


def export_raster_training_images(
    report: dict[str, Any],
    source_path: str | Path,
    output_dir: str | Path,
) -> list[dict[str, Any]]:
    """Copy clean analyzer images and review overlays into a training archive."""
    source = Path(source_path)
    root = Path(output_dir)
    clean_dir = root / "images" / "raster-analyzer"
    overlay_dir = root / "images" / "raster-analyzer-overlays"
    crop_dir = root / "images" / "raster-analyzer-orthographic-crops"
    manifest_path = root / "raster-analyzer-manifest.json"
    samples: list[dict[str, Any]] = []
    prefix = _safe_stem(source.stem)

    for page in report.get("pages", []):
        page_number = int(page.get("page_number", len(samples) + 1))
        raster = page.get("raster_analysis") or {}
        sample_id = f"{prefix}-page-{page_number:03d}"
        clean_path = _copy_if_file(
            raster.get("drawing_views_png"),
            clean_dir / f"{sample_id}.png",
        )
        if clean_path is None:
            render = page.get("render") or {}
            render_directory = report.get("rendering", {}).get("directory")
            render_file = render.get("file")
            fallback_path = (
                Path(render_directory) / str(render_file)
                if render_directory and render_file
                else None
            )
            clean_path = _copy_if_file(
                fallback_path,
                clean_dir / f"{sample_id}.png",
            )
        overlay_path = _copy_if_file(
            raster.get("full_page_overlay_png") or raster.get("overlay_png"),
            overlay_dir / f"{sample_id}.png",
        )
        crop_path = _copy_if_file(
            raster.get("orthographic_extraction", {}).get("path"),
            crop_dir / f"{sample_id}.png",
        )
        if clean_path is None and overlay_path is None and crop_path is None:
            continue
        samples.append(
            {
                "sample_id": sample_id,
                "source_file": str(source),
                "page_number": page_number,
                "clean_image": clean_path,
                "overlay_image": overlay_path,
                "orthographic_crops": crop_path,
                "view_regions": raster.get("view_regions", []),
                "training_note": (
                    "Use clean_image for model input. Overlay and crop files are "
                    "for review and should not be used as model inputs."
                ),
            }
        )

    existing: list[dict[str, Any]] = []
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                existing = loaded
        except json.JSONDecodeError:
            existing = []
    by_id = {str(item.get("sample_id")): item for item in existing if isinstance(item, dict)}
    by_id.update({item["sample_id"]: item for item in samples})
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(sorted(by_id.values(), key=lambda item: item["sample_id"]), indent=2)
        + "\n",
        encoding="utf-8",
    )
    return samples
