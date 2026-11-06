"""Create reviewable COCO pre-annotations from raster view proposals."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import cv2
from partbuilder.core.raster_drawing_analyzer import analyze_raster_drawing


CLASS_NAMES = ("orthographic", "section", "detail", "isometric")
CLASS_IDS = {name: index + 1 for index, name in enumerate(CLASS_NAMES)}
CLASSIFICATION_TO_CLASS = {
    "orthographic": "orthographic",
    "section": "section",
    "detail": "detail",
    "isometric": "isometric",
    "isometric_reference": "isometric",
}


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read manifest '{path}': {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("samples"), list):
        raise ValueError("Manifest must contain a 'samples' list.")
    return manifest


def _image_path(dataset_dir: Path, sample: dict[str, Any]) -> Path:
    relative_path = sample.get("text_masked_image") or sample.get("raw_image")
    if not isinstance(relative_path, str) or not relative_path:
        raise ValueError(f"Sample '{sample.get('sample_id', '<unknown>')}' has no image path.")
    path = dataset_dir / relative_path
    if not path.is_file():
        raise FileNotFoundError(f"Image referenced by manifest does not exist: {path}")
    return path


def _source_bbox(region: dict[str, Any], report: dict[str, Any]) -> dict[str, int] | None:
    layout_box = region.get("bbox_source_px")
    if isinstance(layout_box, dict):
        keys = ("x", "y", "width", "height")
        if all(key in layout_box for key in keys):
            return {key: int(round(float(layout_box[key]))) for key in keys}
    box = region.get("bbox_px")
    if isinstance(box, dict):
        keys = ("x", "y", "width", "height")
        if all(key in box for key in keys):
            crop_offset = report.get("crop_offset_px") or {}
            scale = max(float(report.get("analysis_scale", 1.0)), 1e-9)
            return {
                "x": int(round((float(box["x"]) + float(crop_offset.get("left", 0))) / scale)),
                "y": int(round((float(box["y"]) + float(crop_offset.get("top", 0))) / scale)),
                "width": int(round(float(box["width"]) / scale)),
                "height": int(round(float(box["height"]) / scale)),
            }
    return None


def _valid_bbox(box: dict[str, int], width: int, height: int) -> bool:
    return (
        box["width"] > 1
        and box["height"] > 1
        and 0 <= box["x"] < width
        and 0 <= box["y"] < height
        and box["x"] + box["width"] <= width
        and box["y"] + box["height"] <= height
    )


def preannotate_dataset(dataset_dir: Path, manifest_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = _read_manifest(manifest_path)
    images: list[dict[str, Any]] = []
    annotations: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    image_id = 1
    annotation_id = 1

    for sample in manifest["samples"]:
        if not isinstance(sample, dict):
            raise ValueError("Every manifest sample must be an object.")
        image_path = _image_path(dataset_dir, sample)
        image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise ValueError(f"OpenCV could not decode image: {image_path}")
        height, width = image.shape
        images.append({
            "id": image_id,
            "file_name": str(image_path.relative_to(dataset_dir)).replace("\\", "/"),
            "width": int(width),
            "height": int(height),
            "sample_id": sample.get("sample_id"),
            "source_id": sample.get("source_id"),
            "split": sample.get("split"),
        })

        report = analyze_raster_drawing(image, preserve_full_frame=True)
        layout = report.get("drawing_view_layout") or {}
        regions = layout.get("regions") or report.get("view_regions") or []
        for region in regions:
            if not isinstance(region, dict):
                continue
            classification = str(region.get("classification", "unknown"))
            class_name = CLASSIFICATION_TO_CLASS.get(classification)
            box = _source_bbox(region, report)
            proposal = {
                "sample_id": sample.get("sample_id"),
                "image_id": image_id,
                "classification": classification,
                "mapped_class": class_name,
                "heuristic_score": region.get("heuristic_score"),
                "evidence": region.get("evidence", []),
                "bbox_px": box,
            }
            if class_name is None or box is None or not _valid_bbox(box, width, height):
                proposal["review_reason"] = "unmapped_or_invalid_proposal"
                review.append(proposal)
                continue
            annotations.append({
                "id": annotation_id,
                "image_id": image_id,
                "category_id": CLASS_IDS[class_name],
                "bbox": [box["x"], box["y"], box["width"], box["height"]],
                "area": box["width"] * box["height"],
                "iscrowd": 0,
                "proposal_source": "raster_drawing_analyzer",
                "heuristic_score": region.get("heuristic_score"),
                "needs_review": True,
            })
            review.append(proposal)
            annotation_id += 1
        image_id += 1

    coco = {
        "info": {
            "description": "Reviewable pre-annotations from the deterministic raster analyzer",
            "version": "1",
        },
        "licenses": [],
        "images": images,
        "annotations": annotations,
        "categories": [
            {"id": category_id, "name": name, "supercategory": "drawing_view"}
            for name, category_id in CLASS_IDS.items()
        ],
    }
    return coco, review


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate COCO view proposals for manual review."
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("data/training/view_detection"),
        help="Dataset directory containing manifest.json",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="Manifest path (defaults to DATASET/manifest.json)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="COCO output path (defaults to DATASET/preannotations.json)",
    )
    parser.add_argument(
        "--review-output",
        type=Path,
        help="Review metadata path (defaults to DATASET/preannotations-review.json)",
    )
    args = parser.parse_args(argv)
    dataset_dir = args.dataset
    manifest_path = args.manifest or dataset_dir / "manifest.json"
    output_path = args.output or dataset_dir / "preannotations.json"
    review_path = args.review_output or dataset_dir / "preannotations-review.json"
    try:
        coco, review = preannotate_dataset(dataset_dir, manifest_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(coco, indent=2) + "\n", encoding="utf-8")
        review_path.write_text(json.dumps(review, indent=2) + "\n", encoding="utf-8")
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Created {len(coco['annotations'])} COCO proposal(s).")
    print(f"Review every box before training: {output_path}")
    print(f"Proposal details: {review_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
