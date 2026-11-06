# Drawing View Starter Data

This bootstrap set is for the first model: detecting and classifying drawing
views before dimension extraction.

It contains four source drawings and 17 view regions, labeled as orthographic,
section, detail, or isometric. The PNGs are clean page images rendered from the
fixture PDFs; colored analyzer boxes are not part of the model inputs. The
labels were checked against the drawing pages. Bounding boxes originate from
the raster analyzer's source-coordinate proposals and can include callouts, so
review and tighten them before using this set for a production detector.

`annotations.json` stores page-level samples. Keep all views from one drawing
together when creating train, validation, and test splits. This starter set
places all four drawings in `train`; it is too small to measure generalization.
It has only one detail-view example, so more independently sourced drawings
with section and detail views are needed before training a useful classifier.

The source PDFs are existing local fixtures. Check their usage terms before
redistributing the images or using them outside this local prototype.

## Preparing new drawings

From the `sw_part_builder` directory, run:

```powershell
python prepare_view_dataset.py ..\path\to\drawings --output data\training\view_detection
```

The input can be a directory containing PDF, PNG, JPG, TIFF, or BMP files.
PDF pages are rendered at 300 DPI by default. The script writes:

```text
images/raw/          original rendered pages
images/text-masked/  copies with OCR-detected text boxes painted white
ocr/                 Tesseract reports for review
manifest.json        drawing-level train/val/test assignments
```

Tesseract must be installed as an application and available on `PATH`, or
configured with `ALIA_TESSERACT_CMD`. Annotate the `images/text-masked` files
in CVAT or Label Studio with:

```text
orthographic
section
detail
isometric
```

Review and tighten every view box before exporting COCO annotations. Do not
split pages from one source drawing across train, validation, and test.

## Validate the CVAT export

Place the exported COCO JSON in `cvat_export/instances_default.json`. The
original masked images should remain under `images/text-masked/`. Validate the
paths and boxes before training:

```powershell
python validate_view_annotations.py `
  data/training/view_detection/cvat_export/instances_default.json `
  --image-root data/training/view_detection
```

The export may contain fewer pages than the original dataset if pages were
deleted in CVAT. That is expected. The validator must report `Validation
passed.` before training.

## Train the first detector

If `manifest.json` is available, pass it so pages from the same source drawing
stay together. Otherwise the training script creates a deterministic fallback
split:

```powershell
python train_view_detector.py `
  --annotations data/training/view_detection/cvat_export/instances_default.json `
  --image-root data/training/view_detection `
  --manifest data/training/view_detection/manifest.json `
  --output data/training/view_detection/view_detector.pt `
  --epochs 10
```

The first run downloads torchvision's pretrained Faster R-CNN weights. A GPU
is used automatically when CUDA is available; otherwise training runs on the
CPU and may take substantially longer.

## Automatic pre-annotations

To generate proposals from the analyzer instead of drawing every box from
scratch, run:

```powershell
python preannotate_view_dataset.py
```

The command writes `preannotations.json` in COCO format and
`preannotations-review.json` with the analyzer evidence. Import
`preannotations.json` into CVAT, then review, move, resize, relabel, or delete
every proposal. These are starting points only; they must not be used as
ground-truth labels without review.