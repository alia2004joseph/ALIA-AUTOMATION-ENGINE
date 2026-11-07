import json

from partbuilder.core.raster_training_export import export_raster_training_images


def test_export_raster_training_images_archives_clean_and_review_files(tmp_path):
    source = tmp_path / "practice drawing.pdf"
    source.write_bytes(b"pdf placeholder")
    analysis_dir = tmp_path / "analysis"
    clean = analysis_dir / "page-001-drawing-view-layout.png"
    overlay = analysis_dir / "page-001-full-page-overlay.png"
    crop = analysis_dir / "page-001-orthographic-views.png"
    analysis_dir.mkdir()
    for path in (clean, overlay, crop):
        path.write_bytes(path.name.encode())

    report = {
        "rendering": {"directory": str(analysis_dir)},
        "pages": [{
            "page_number": 1,
            "raster_analysis": {
                "drawing_views_png": str(clean),
                "full_page_overlay_png": str(overlay),
                "orthographic_extraction": {"path": str(crop)},
                "view_regions": [{"classification": "orthographic"}],
            },
        }],
    }

    exports = export_raster_training_images(report, source, tmp_path / "dataset")

    assert len(exports) == 1
    assert (tmp_path / "dataset/images/raster-analyzer/practice_drawing-page-001.png").is_file()
    assert (tmp_path / "dataset/images/raster-analyzer-overlays/practice_drawing-page-001.png").is_file()
    assert (
        tmp_path
        / "dataset/images/raster-analyzer-orthographic-crops/practice_drawing-page-001.png"
    ).is_file()
    manifest = json.loads(
        (tmp_path / "dataset/raster-analyzer-manifest.json").read_text()
    )
    assert manifest[0]["sample_id"] == "practice_drawing-page-001"
