"""Evaluate a trained view detector and save visual test predictions."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import torch
from PIL import Image, ImageDraw
from torchvision.transforms.functional import pil_to_tensor

from train_view_detector import (
    CLASS_NAMES,
    _build_model,
    _load_coco,
    _split_ids,
)


def _iou(first: list[float], second: list[float]) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    first_area = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
    second_area = max(0.0, second[2] - second[0]) * max(0.0, second[3] - second[1])
    union = first_area + second_area - intersection
    return intersection / union if union else 0.0


def _xyxy(annotation: dict[str, Any]) -> list[float]:
    x, y, width, height = (float(value) for value in annotation["bbox"])
    return [x, y, x + width, y + height]


def _match_predictions(
    predictions: dict[str, list[Any]],
    truths: dict[str, list[list[float]]],
    threshold: float,
) -> tuple[int, int, int]:
    true_positive = false_positive = false_negative = 0
    for class_name in CLASS_NAMES[1:]:
        remaining = list(truths.get(class_name, []))
        for prediction in sorted(predictions.get(class_name, []), reverse=True):
            best_index = -1
            best_iou = 0.0
            for index, truth in enumerate(remaining):
                overlap = _iou(prediction[1], truth)
                if overlap > best_iou:
                    best_iou = overlap
                    best_index = index
            if best_index >= 0 and best_iou >= threshold:
                true_positive += 1
                remaining.pop(best_index)
            else:
                false_positive += 1
        false_negative += len(remaining)
    return true_positive, false_positive, false_negative


def _draw_preview(
    image_path: Path,
    output_path: Path,
    predictions: list[tuple[str, float, list[float]]],
    truths: list[tuple[str, list[float]]],
) -> None:
    with Image.open(image_path) as source:
        image = source.convert("RGB")
    draw = ImageDraw.Draw(image)
    for label, box in truths:
        draw.rectangle(box, outline="lime", width=8)
        draw.text((box[0] + 4, box[1] + 4), f"GT {label}", fill="lime")
    for label, score, box in predictions:
        draw.rectangle(box, outline="red", width=8)
        draw.text((box[0] + 4, max(0, box[1] - 25)), f"{label} {score:.2f}", fill="red")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.thumbnail((1600, 1600))
    image.save(output_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--image-root", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("view-evaluation"))
    parser.add_argument("--score-threshold", type=float, default=0.50)
    parser.add_argument("--iou-threshold", type=float, default=0.50)
    parser.add_argument("--max-previews", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)

    data = _load_coco(args.annotations)
    category_names = {
        int(category["id"]): str(category["name"])
        for category in data["categories"]
    }
    split_ids = _split_ids(data, args.manifest, args.seed, 0.15, 0.15)
    test_ids = split_ids["test"]
    if not test_ids:
        raise ValueError("No test images were found.")
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = _build_model(len(CLASS_NAMES)).to(device)
    checkpoint = torch.load(args.checkpoint, map_location=device)
    model.load_state_dict(checkpoint["model"])
    model.eval()

    image_by_id = {int(image["id"]): image for image in data["images"]}
    truth_by_id: dict[int, list[tuple[str, list[float]]]] = {}
    for annotation in data["annotations"]:
        image_id = int(annotation["image_id"])
        if image_id not in test_ids:
            continue
        truth_by_id.setdefault(image_id, []).append(
            (category_names[int(annotation["category_id"])], _xyxy(annotation))
        )

    totals = {"true_positive": 0, "false_positive": 0, "false_negative": 0}
    preview_count = 0
    with torch.inference_mode():
        for image_id in test_ids:
            image_info = image_by_id[image_id]
            image_path = args.image_root / Path(str(image_info["file_name"]))
            with Image.open(image_path) as source:
                image = source.convert("RGB")
            tensor = pil_to_tensor(image).float().div(255).to(device)
            result = model([tensor])[0]
            predictions: dict[str, list[tuple[float, list[float]]]] = {}
            preview_predictions: list[tuple[str, float, list[float]]] = []
            for box, label, score in zip(
                result["boxes"].cpu().tolist(),
                result["labels"].cpu().tolist(),
                result["scores"].cpu().tolist(),
            ):
                if score < args.score_threshold or label >= len(CLASS_NAMES):
                    continue
                class_name = CLASS_NAMES[label]
                predictions.setdefault(class_name, []).append((score, box))
                preview_predictions.append((class_name, score, box))
            tp, fp, fn = _match_predictions(
                predictions, 
                {
                    name: [box for label, box in truth_by_id.get(image_id, []) if label == name]
                    for name in CLASS_NAMES[1:]
                },
                args.iou_threshold,
            )
            totals["true_positive"] += tp
            totals["false_positive"] += fp
            totals["false_negative"] += fn
            if preview_count < args.max_previews:
                _draw_preview(
                    image_path,
                    args.output_dir / "previews" / image_path.name,
                    preview_predictions,
                    truth_by_id.get(image_id, []),
                )
                preview_count += 1

    precision = totals["true_positive"] / max(
        1, totals["true_positive"] + totals["false_positive"]
    )
    recall = totals["true_positive"] / max(
        1, totals["true_positive"] + totals["false_negative"]
    )
    metrics = {
        "device": str(device),
        "test_images": len(test_ids),
        "score_threshold": args.score_threshold,
        "iou_threshold": args.iou_threshold,
        **totals,
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / max(1e-12, precision + recall),
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(metrics, indent=2))
    print(f"Preview images: {args.output_dir / 'previews'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
