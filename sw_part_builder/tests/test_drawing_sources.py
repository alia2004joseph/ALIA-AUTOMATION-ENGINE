import os

import pytest

from partbuilder.core.drawing_interpreter import interpret_drawing
from partbuilder.core.drawing_sources import DrawingSourceError, inspect_drawing_source


def test_raster_image_uses_common_report_and_preserves_no_scale_claim(tmp_path):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    image_path = tmp_path / "scan.png"
    image = np.full((300, 420), 255, dtype=np.uint8)
    cv2.rectangle(image, (80, 60), (260, 230), 0, 3)
    assert cv2.imwrite(str(image_path), image)

    output_dir = tmp_path / "analysis"
    report = inspect_drawing_source(image_path, output_dir=output_dir)
    interpretation = interpret_drawing(report)

    assert report["input_type"] == "raster_image"
    assert report["pages"][0]["raster_analysis"]["status"] == "ok"
    assert report["pages"][0]["coordinate_system"]["unit"] == "pixels"
    assert report["pages"][0]["scale_evidence"]["status"] == "unknown"
    assert (output_dir / "page-001.png").is_file()
    assert interpretation["input_type"] == "raster_image"
    assert interpretation["pages"][0]["dimensions"] == []


def test_default_raster_output_does_not_overwrite_source_named_page_png(tmp_path):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    image_path = tmp_path / "page-001.png"
    image = np.full((100, 140), 255, dtype=np.uint8)
    assert cv2.imwrite(str(image_path), image)
    original = image_path.read_bytes()

    report = inspect_drawing_source(image_path)

    output_dir = tmp_path / "page-001-drawing-analysis"
    assert image_path.read_bytes() == original
    assert (output_dir / "page-001.png").is_file()
    assert report["rendering"]["directory"] == str(output_dir)


def test_offline_tesseract_text_is_exposed_as_unverified_dimension_candidate(tmp_path):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    from partbuilder.core.offline_ocr import get_offline_ocr_status

    if get_offline_ocr_status()["status"] != "available":
        pytest.skip("Offline Tesseract engine is not installed.")

    image_path = tmp_path / "dimension-scan.png"
    image = np.full((220, 900), 255, dtype=np.uint8)
    cv2.putText(
        image, "25.0 MM", (35, 145), cv2.FONT_HERSHEY_SIMPLEX,
        3.2, 0, 7, cv2.LINE_AA,
    )
    assert cv2.imwrite(str(image_path), image)

    report = inspect_drawing_source(image_path, run_ocr=True)
    interpretation = interpret_drawing(report)

    assert report["pages"][0]["offline_ocr"]["status"] == "completed"
    assert report["summary"]["ocr_word_count"] >= 2
    dimension = next(
        item for item in interpretation["pages"][0]["dimensions"]
        if item["dimension_type"] == "linear"
    )
    assert dimension["nominal_value"] == "25.0"
    assert dimension["unit"] == "mm"
    assert dimension["recognition_method"] == "offline_tesseract"
    assert dimension["recognition_confidence"] is None
    assert dimension["confidence_status"] == "uncalibrated"
    assert dimension["association_status"] in {"tentative", "unassigned"}
    assert dimension["candidate_view_id"] is None or dimension["review_required"]
    assert dimension["review_required"] is True


def test_dxf_inspection_preserves_entities_and_declared_units(tmp_path):
    ezdxf = pytest.importorskip("ezdxf")
    from ezdxf import units

    path = tmp_path / "part.dxf"
    document = ezdxf.new()
    document.units = units.MM
    modelspace = document.modelspace()
    modelspace.add_lwpolyline([(0, 0), (40, 0), (40, 20), (0, 20)], close=True)
    modelspace.add_circle((10, 10), radius=3)
    document.saveas(path)

    report = inspect_drawing_source(path)
    interpretation = interpret_drawing(report)
    page = interpretation["pages"][0]

    assert report["input_type"] == "vector_dxf"
    assert report["source_document"]["units"] == "Millimeters"
    assert page["units_evidence"]["status"] == "declared"
    assert [entity["type"] for entity in page["vector_entities"]] == [
        "LWPOLYLINE", "CIRCLE",
    ]
    assert page["dimensions"] == []
    assert page["vector_entities"][0]["closed"] is True


def test_pdf_uses_existing_inspector_and_common_metadata(tmp_path):
    fixture = os.path.join(os.path.dirname(__file__), "alia_test_blueprint.pdf")
    report = inspect_drawing_source(fixture, dpi=120, output_dir=tmp_path)
    interpretation = interpret_drawing(report)

    assert report["input_type"] == "pdf"
    assert interpretation["input_type"] == "pdf"
    assert interpretation["source_document"]["format"] == "pdf"
    assert (tmp_path / "page-001.png").is_file()


def test_scanned_pdf_ocr_uses_raster_source_coordinates(tmp_path):
    from partbuilder.core.offline_ocr import get_offline_ocr_status

    if os.environ.get("ALIA_RUN_DRAWING_REFERENCE_TESTS") != "1":
        pytest.skip(
            "Set ALIA_RUN_DRAWING_REFERENCE_TESTS=1 to run local reference-fixture tests."
        )
    if get_offline_ocr_status()["status"] != "available":
        pytest.skip("Offline Tesseract engine is not installed.")
    fixture = os.path.join(
        os.path.dirname(__file__), "fixtures", "scanned_alia.pdf"
    )
    if not os.path.isfile(fixture):
        pytest.skip("Optional scanned drawing reference fixture is not included.")
    report = inspect_drawing_source(fixture, output_dir=tmp_path, run_ocr=True)
    interpretation = interpret_drawing(report)

    assert report["input_type"] == "pdf"
    assert report["offline_ocr"]["status"] == "completed"
    assert report["summary"]["ocr_word_count"] > 0
    page = interpretation["pages"][0]
    ocr_dimensions = [
        item for item in page["dimensions"]
        if item["recognition_method"] == "offline_tesseract"
    ]
    assert all(item["recognition_confidence"] is None for item in ocr_dimensions)
    assert all(item["review_required"] for item in ocr_dimensions)
    assert all(
        "bbox_pt" in item and "bbox_px" not in item
        for item in ocr_dimensions
    )


def test_unsupported_source_has_actionable_error(tmp_path):
    source = tmp_path / "drawing.svg"
    source.write_text("<svg/>", encoding="utf-8")

    with pytest.raises(DrawingSourceError, match="Unsupported drawing format"):
        inspect_drawing_source(source)
