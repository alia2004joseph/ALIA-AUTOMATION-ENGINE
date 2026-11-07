"""Load and run the trained view detector on analyzer images."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


CLASS_NAMES = ("__background__", "orthographic", "section", "detail", "isometric")


def _build_model(num_classes: int):
    import torch
    from torchvision.models.detection import (
        FasterRCNN_ResNet50_FPN_Weights,
        fasterrcnn_resnet50_fpn,
    )
    from torchvision.models.detection.faster_rcnn import FastRCNNPredictor

    model = fasterrcnn_resnet50_fpn(weights=None)
    in_features = model.roi_heads.box_predictor.cls_score.in_features
    model.roi_heads.box_predictor = FastRCNNPredictor(in_features, num_classes)
    return model


def evaluate_image(
    image_path: str | Path,
    checkpoint_path: str | Path,
    output_path: str | Path,
    *,
    score_threshold: float = 0.50,
) -> dict[str, Any]:
    """Run inference, save a labeled preview, and return predictions."""
    import torch
    from PIL import Image, ImageDraw
    from torchvision.transforms.functional import pil_to_tensor

    image_path = Path(image_path)
    checkpoint_path = Path(checkpoint_path)
    output_path = Path(output_path)
    if not image_path.is_file():
        raise FileNotFoundError(f"Detector image does not exist: {image_path}")
    if not checkpoint_path.is_file():
        raise FileNotFoundError(f"Detector checkpoint does not exist: {checkpoint_path}")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = _build_model(len(CLASS_NAMES)).to(device)
    checkpoint = torch.load(checkpoint_path, map_location=device)
    if not isinstance(checkpoint, dict) or "model" not in checkpoint:
        raise ValueError(f"Unsupported detector checkpoint: {checkpoint_path}")
    model.load_state_dict(checkpoint["model"])
    model.eval()

    with Image.open(image_path) as source:
        image = source.convert("RGB")
    tensor = pil_to_tensor(image).float().div(255).to(device)
    with torch.inference_mode():
        result = model([tensor])[0]

    predictions: list[dict[str, Any]] = []
    preview = image.copy()
    draw = ImageDraw.Draw(preview)
    for box, label, score in zip(
        result["boxes"].cpu().tolist(),
        result["labels"].cpu().tolist(),
        result["scores"].cpu().tolist(),
    ):
        if score < score_threshold or label <= 0 or label >= len(CLASS_NAMES):
            continue
        class_name = CLASS_NAMES[label]
        box = [float(value) for value in box]
        predictions.append(
            {"class": class_name, "score": float(score), "box_xyxy": box}
        )
        draw.rectangle(box, outline="red", width=max(2, image.width // 500))
        draw.text(
            (box[0] + 4, max(0, box[1] - 18)),
            f"{class_name} {score:.2f}",
            fill="red",
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    preview.save(output_path)
    return {
        "status": "ok",
        "device": str(device),
        "checkpoint": str(checkpoint_path),
        "image": str(image_path),
        "score_threshold": score_threshold,
        "predictions": predictions,
        "overlay": str(output_path),
    }


def evaluate_report(
    report: dict[str, Any],
    checkpoint_path: str | Path,
    output_dir: str | Path,
    *,
    score_threshold: float = 0.50,
) -> dict[str, Any]:
    """Evaluate every clean raster page in an analyzer report."""
    output_dir = Path(output_dir)
    pages: list[dict[str, Any]] = []
    for page in report.get("pages", []):
        image_path = (page.get("raster_analysis") or {}).get("drawing_views_png")
        if not image_path:
            continue
        image_path = Path(image_path)
        if not image_path.is_file():
            continue
        result = evaluate_image(
            image_path,
            checkpoint_path,
            output_dir / f"page-{int(page['page_number']):03d}-view-detector.png",
            score_threshold=score_threshold,
        )
        pages.append(
            {
                "page_number": int(page["page_number"]),
                "overlay": result["overlay"],
                "prediction_count": len(result["predictions"]),
                "predictions": result["predictions"],
            }
        )
        page.setdefault("raster_analysis", {})["view_detector_overlay_png"] = result["overlay"]
        page["raster_analysis"]["view_detector_predictions"] = result["predictions"]
    result = {
        "status": "ok",
        "checkpoint": str(checkpoint_path),
        "score_threshold": score_threshold,
        "pages": pages,
        "prediction_count": sum(page["prediction_count"] for page in pages),
    }
    (output_dir / "view-detector-evaluation.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    return result
