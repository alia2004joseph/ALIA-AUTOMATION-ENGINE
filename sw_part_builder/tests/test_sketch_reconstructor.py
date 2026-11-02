import numpy as np
import pytest

from partbuilder.core.sketch_reconstructor import (
    SketchReconstructionError,
    orthographic_reconstruction_candidates,
    reconstruct_raster_profile,
)


def _view_report(classification="orthographic"):
    return {
        "analysis_scale": 1.0,
        "crop_offset_px": {"left": 0, "top": 0},
        "view_regions": [{
            "view_id": "view-01",
            "classification": classification,
            "bbox_px": {"x": 45, "y": 35, "width": 210, "height": 170},
        }],
    }


def _rectangle_image():
    cv2 = pytest.importorskip("cv2")
    image = np.full((240, 300), 255, dtype=np.uint8)
    cv2.rectangle(image, (60, 50), (240, 190), 0, 3)
    return image


def test_reconstructs_one_closed_outline_using_user_confirmed_width():
    result = reconstruct_raster_profile(
        _rectangle_image(),
        _view_report(),
        "view-01",
        90,
    )

    assert result["profile_type"] == "polygon"
    assert result["calibration"] == "user_confirmed_overall_width"
    assert result["width_mm"] == 90
    assert result["height_mm"] == pytest.approx(70, abs=1)
    assert result["vertex_count"] == 4
    points = result["shape"]["points"]
    assert max(point[0] for point in points) - min(point[0] for point in points) == pytest.approx(90)
    assert result["preview_rgb"].ndim == 3


def test_reconstructs_clean_circular_profile_as_circle():
    cv2 = pytest.importorskip("cv2")
    image = np.full((240, 300), 255, dtype=np.uint8)
    cv2.circle(image, (150, 120), 60, 0, 3)
    report = _view_report()
    report["view_regions"][0]["bbox_px"] = {
        "x": 80, "y": 50, "width": 140, "height": 140,
    }

    result = reconstruct_raster_profile(image, report, "view-01", 60)

    assert result["profile_type"] == "circle"
    assert result["shape"]["diameter"] == pytest.approx(60, abs=1)


def test_rejects_internal_opening_instead_of_silently_dropping_it():
    cv2 = pytest.importorskip("cv2")
    image = _rectangle_image()
    cv2.circle(image, (150, 120), 20, 0, 3)

    with pytest.raises(SketchReconstructionError, match="internal loop or opening"):
        reconstruct_raster_profile(image, _view_report(), "view-01", 90)


def test_rejects_multiple_disconnected_profiles():
    cv2 = pytest.importorskip("cv2")
    image = _rectangle_image()
    cv2.circle(image, (275, 120), 24, 0, 3)
    report = _view_report()
    report["view_regions"][0]["bbox_px"]["width"] = 245

    with pytest.raises(SketchReconstructionError, match="multiple separate outlines"):
        reconstruct_raster_profile(image, report, "view-01", 90)


@pytest.mark.parametrize("width", [0, -1, float("nan"), "not a number"])
def test_requires_positive_finite_calibration_width(width):
    with pytest.raises(SketchReconstructionError, match="width"):
        reconstruct_raster_profile(_rectangle_image(), _view_report(), "view-01", width)


def test_rejects_isometric_view_as_sketch_source():
    with pytest.raises(SketchReconstructionError, match="Only a candidate marked orthographic"):
        reconstruct_raster_profile(
            _rectangle_image(),
            _view_report("isometric_reference"),
            "view-01",
            90,
        )


def test_excludes_views_reviewed_as_non_orthographic():
    raster = _view_report()
    interpretation_page = {
        "views": [{
            "view_id": "view-01",
            "review": {"view_type": "isometric_reference"},
        }],
    }

    assert orthographic_reconstruction_candidates(
        raster, interpretation_page
    ) == []


def test_allows_view_after_human_review_confirms_orthographic_type():
    raster = _view_report()
    interpretation_page = {
        "views": [{
            "view_id": "view-01",
            "review": {"view_type": "orthographic"},
        }],
    }

    assert orthographic_reconstruction_candidates(
        raster, interpretation_page
    ) == raster["view_regions"]


def test_reconstructed_profile_can_drive_existing_part_preview():
    pytest.importorskip("cadquery")
    from partbuilder.backends.preview_cq import build_preview
    from partbuilder.core.model import Extrude, Part, Polygon, Sketch
    from partbuilder.core.validate import has_errors, validate

    result = reconstruct_raster_profile(
        _rectangle_image(),
        _view_report(),
        "view-01",
        90,
    )
    sketch = Sketch(
        name="Reconstructed",
        entities=[Polygon(points=[tuple(point) for point in result["shape"]["points"]])],
    )
    part = Part(features=[
        sketch,
        Extrude(name="PreviewSolid", sketch="Reconstructed", depth=5),
    ])

    assert not has_errors(validate(part))
    preview = build_preview(part, tessellate=False)
    assert preview.body is not None
    assert preview.volumes["PreviewSolid"] > 0
