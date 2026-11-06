"""Validate a COCO view-detection export against local dataset images."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


EXPECTED_CLASSES = {"orthographic", "section", "detail", "isometric"}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read JSON '{path}': {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"COCO file must contain an object: {path}")
    return value


def validate_annotations(
    annotations_path: Path,
    image_root: Path,
) -> tuple[list[str], dict[str, int]]:
    data = _read_json(annotations_path)
    images = data.get("images")
    annotations = data.get("annotations")
    categories = data.get("categories")
    if not isinstance(images, list) or not isinstance(annotations, list):
        raise ValueError("COCO JSON must contain 'images' and 'annotations' lists.")
    if not isinstance(categories, list):
        raise ValueError("COCO JSON must contain a 'categories' list.")

    category_names = {
        int(category["id"]): str(category["name"])
        for category in categories
        if isinstance(category, dict) and "id" in category and "name" in category
    }
    missing_categories = EXPECTED_CLASSES - set(category_names.values())
    if missing_categories:
        raise ValueError(f"Missing expected categories: {sorted(missing_categories)}")

    errors: list[str] = []
    image_ids: set[int] = set()
    image_sizes: dict[int, tuple[int, int]] = {}
    for image in images:
        if not isinstance(image, dict):
            errors.append("An image entry is not an object.")
            continue
        try:
            image_id = int(image["id"])
            width = int(image["width"])
            height = int(image["height"])
            file_name = str(image["file_name"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"Invalid image entry: {image!r}")
            continue
        if image_id in image_ids:
            errors.append(f"Duplicate image id: {image_id}")
        image_ids.add(image_id)
        image_sizes[image_id] = (width, height)
        path = image_root / Path(file_name)
        if not path.is_file():
            errors.append(f"Missing image for id {image_id}: {path}")

    annotation_counts = {name: 0 for name in EXPECTED_CLASSES}
    for annotation in annotations:
        if not isinstance(annotation, dict):
            errors.append("An annotation entry is not an object.")
            continue
        try:
            image_id = int(annotation["image_id"])
            category_id = int(annotation["category_id"])
            bbox = annotation["bbox"]
            x, y, width, height = (float(value) for value in bbox)
        except (KeyError, TypeError, ValueError):
            errors.append(f"Invalid annotation entry: {annotation!r}")
            continue
        category_name = category_names.get(category_id)
        if category_name not in EXPECTED_CLASSES:
            errors.append(f"Unknown category id {category_id} in annotation.")
            continue
        annotation_counts[category_name] += 1
        if image_id not in image_sizes:
            errors.append(f"Annotation references unknown image id: {image_id}")
            continue
        image_width, image_height = image_sizes[image_id]
        if width <= 0 or height <= 0:
            errors.append(f"Non-positive bbox in annotation {annotation.get('id')}.")
        if x < 0 or y < 0 or x + width > image_width or y + height > image_height:
            errors.append(
                f"Out-of-bounds bbox in annotation {annotation.get('id')} "
                f"for image {image_id}: {bbox}"
            )

    return errors, annotation_counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("annotations", type=Path, help="COCO JSON export")
    parser.add_argument(
        "--image-root",
        type=Path,
        required=True,
        help="Dataset directory containing text-masked/ or the JSON paths",
    )
    args = parser.parse_args(argv)
    try:
        errors, counts = validate_annotations(args.annotations, args.image_root)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Checked annotation file: {args.annotations}")
    print(f"Annotations by class: {counts}")
    if errors:
        print(f"Validation failed with {len(errors)} error(s):", file=sys.stderr)
        for error in errors[:20]:
            print(f"- {error}", file=sys.stderr)
        if len(errors) > 20:
            print(f"- ... and {len(errors) - 20} more", file=sys.stderr)
        return 1
    print("Validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
