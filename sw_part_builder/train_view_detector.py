"""Train a torchvision Faster R-CNN model from a COCO view export."""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any

import torch
from PIL import Image
from torch.utils.data import DataLoader, Dataset
from torchvision.models.detection import (
    FasterRCNN_ResNet50_FPN_Weights,
    fasterrcnn_resnet50_fpn,
)
from torchvision.models.detection.faster_rcnn import FastRCNNPredictor
from torchvision.transforms.functional import pil_to_tensor


CLASS_NAMES = ("__background__", "orthographic", "section", "detail", "isometric")


def _load_coco(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _split_ids(
    data: dict[str, Any],
    manifest_path: Path | None,
    seed: int,
    val_ratio: float,
    test_ratio: float,
) -> dict[str, list[int]]:
    images = data["images"]
    if manifest_path and manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        by_sample = {
            str(sample["sample_id"]): str(sample["split"])
            for sample in manifest.get("samples", [])
            if isinstance(sample, dict) and "sample_id" in sample and "split" in sample
        }
        split_ids = {"train": [], "val": [], "test": []}
        for image in images:
            sample_id = str(image.get("sample_id") or Path(image["file_name"]).stem)
            split = by_sample.get(sample_id)
            if split in split_ids:
                split_ids[split].append(int(image["id"]))
        if sum(len(ids) for ids in split_ids.values()) == len(images):
            return split_ids

    ids = [int(image["id"]) for image in images]
    random.Random(seed).shuffle(ids)
    test_count = round(len(ids) * test_ratio)
    val_count = round(len(ids) * val_ratio)
    return {
        "test": ids[:test_count],
        "val": ids[test_count:test_count + val_count],
        "train": ids[test_count + val_count:],
    }


class CocoViewDataset(Dataset[tuple[torch.Tensor, dict[str, torch.Tensor]]]):
    def __init__(
        self,
        data: dict[str, Any],
        image_root: Path,
        image_ids: list[int],
        category_ids: dict[int, int],
    ) -> None:
        self.images = {
            int(image["id"]): image
            for image in data["images"]
            if int(image["id"]) in set(image_ids)
        }
        self.image_ids = image_ids
        self.annotations: dict[int, list[dict[str, Any]]] = {}
        for annotation in data["annotations"]:
            self.annotations.setdefault(int(annotation["image_id"]), []).append(annotation)
        self.image_root = image_root
        self.category_ids = category_ids

    def __len__(self) -> int:
        return len(self.image_ids)

    def __getitem__(self, index: int) -> tuple[torch.Tensor, dict[str, torch.Tensor]]:
        image_id = self.image_ids[index]
        image_info = self.images[image_id]
        image_path = self.image_root / Path(str(image_info["file_name"]))
        with Image.open(image_path) as image:
            image_tensor = pil_to_tensor(image.convert("RGB")).float() / 255.0
        boxes: list[list[float]] = []
        labels: list[int] = []
        areas: list[float] = []
        for annotation in self.annotations.get(image_id, []):
            x, y, width, height = (float(value) for value in annotation["bbox"])
            boxes.append([x, y, x + width, y + height])
            labels.append(self.category_ids[int(annotation["category_id"])])
            areas.append(width * height)
        target = {
            "boxes": torch.tensor(boxes, dtype=torch.float32).reshape(-1, 4),
            "labels": torch.tensor(labels, dtype=torch.int64),
            "image_id": torch.tensor([image_id]),
            "area": torch.tensor(areas, dtype=torch.float32),
            "iscrowd": torch.zeros(len(boxes), dtype=torch.int64),
        }
        return image_tensor, target


def _collate(
    batch: list[tuple[torch.Tensor, dict[str, torch.Tensor]]],
) -> tuple[list[torch.Tensor], list[dict[str, torch.Tensor]]]:
    images, targets = zip(*batch)
    return list(images), list(targets)


def _build_model(num_classes: int) -> torch.nn.Module:
    weights = FasterRCNN_ResNet50_FPN_Weights.DEFAULT
    model = fasterrcnn_resnet50_fpn(weights=weights)
    in_features = model.roi_heads.box_predictor.cls_score.in_features
    model.roi_heads.box_predictor = FastRCNNPredictor(in_features, num_classes)
    return model


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--image-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path, default=Path("view_detector.pt"))
    parser.add_argument("--epochs", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--learning-rate", type=float, default=0.005)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)

    random.seed(args.seed)
    torch.manual_seed(args.seed)
    data = _load_coco(args.annotations)
    category_ids = {
        int(category["id"]): CLASS_NAMES.index(str(category["name"]))
        for category in data["categories"]
    }
    split_ids = _split_ids(data, args.manifest, args.seed, 0.15, 0.15)
    if not split_ids["train"] or not split_ids["val"]:
        raise ValueError("Training and validation splits must both contain images.")
    datasets = {
        split: CocoViewDataset(data, args.image_root, ids, category_ids)
        for split, ids in split_ids.items()
        if ids
    }
    loaders = {
        split: DataLoader(
            dataset,
            batch_size=args.batch_size,
            shuffle=split == "train",
            collate_fn=_collate,
        )
        for split, dataset in datasets.items()
    }
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = _build_model(len(CLASS_NAMES)).to(device)
    optimizer = torch.optim.SGD(
        [parameter for parameter in model.parameters() if parameter.requires_grad],
        lr=args.learning_rate,
        momentum=0.9,
        weight_decay=0.0005,
    )
    for epoch in range(args.epochs):
        model.train()
        total_loss = 0.0
        for images, targets in loaders["train"]:
            images = [image.to(device) for image in images]
            targets = [{key: value.to(device) for key, value in target.items()} for target in targets]
            losses = model(images, targets)
            loss = sum(losses.values())
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total_loss += float(loss.detach().cpu())
        print(f"epoch {epoch + 1}/{args.epochs} train_loss={total_loss:.4f}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "model": model.state_dict(),
            "classes": list(CLASS_NAMES),
            "splits": {split: len(ids) for split, ids in split_ids.items()},
        },
        args.output,
    )
    print(f"Saved model: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
