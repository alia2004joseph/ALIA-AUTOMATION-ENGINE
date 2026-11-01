import os
from pathlib import Path

import pytest

from partbuilder.core.pdf_inspector import PdfInspectionError, inspect_pdf


PDF_FIXTURE = os.path.join(os.path.dirname(__file__), "alia_test_blueprint.pdf")
RASTER_PDF_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "practice_problems_1_1_eng.pdf"
)
RASTER_OVERLAY_REFERENCE = os.path.join(
    os.path.dirname(__file__),
    "fixtures",
    "practice_problems_1_1_eng_raster_overlay.png",
)
RASTER_PDF_FIXTURE_1_2 = os.path.join(
    os.path.dirname(__file__), "fixtures", "practice_problems_1_2_eng.pdf"
)
RASTER_OVERLAY_REFERENCE_1_2 = os.path.join(
    os.path.dirname(__file__),
    "fixtures",
    "practice_problems_1_2_eng_raster_overlay.png",
)
RASTER_PDF_FIXTURE_1_20 = os.path.join(
    os.path.dirname(__file__), "fixtures", "practice_problems_1_20_eng.pdf"
)
RASTER_OVERLAY_REFERENCE_1_20 = os.path.join(
    os.path.dirname(__file__),
    "fixtures",
    "practice_problems_1_20_eng_raster_overlay.png",
)
SCANNED_PDF_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "scanned_alia.pdf"
)
SCANNED_OVERLAY_REFERENCE = os.path.join(
    os.path.dirname(__file__), "fixtures", "scanned_alia_raster_overlay.png"
)


def _require_optional_reference_fixtures():
    if os.environ.get("ALIA_RUN_DRAWING_REFERENCE_TESTS") != "1":
        pytest.skip(
            "Set ALIA_RUN_DRAWING_REFERENCE_TESTS=1 to run local reference-fixture tests."
        )


def test_inspect_existing_vector_pdf_reports_page_and_route():
    pytest.importorskip("pdfplumber")

    report = inspect_pdf(PDF_FIXTURE)

    assert report["status"] == "ok"
    assert report["page_count"] == 1
    assert report["pages"][0]["width_pt"] == pytest.approx(612)
    assert report["pages"][0]["height_pt"] == pytest.approx(792)
    assert report["pages"][0]["content_type"] == "vector"
    assert report["pages"][0]["processing_route"] == "vector-first"
    assert report["summary"]["vector_entity_count"] > 0
    assert report["coordinate_system"]["unit"] == "PDF points"


def test_inspect_pdf_rejects_missing_and_non_pdf_inputs(tmp_path):
    with pytest.raises(PdfInspectionError, match="does not exist"):
        inspect_pdf(tmp_path / "missing.pdf")

    text_file = tmp_path / "drawing.txt"
    text_file.write_text("not a PDF", encoding="utf-8")
    with pytest.raises(PdfInspectionError, match="must be a PDF"):
        inspect_pdf(text_file)


def test_inspect_pdf_renders_pages_when_requested(tmp_path):
    pytest.importorskip("pdfplumber")
    pytest.importorskip("pypdfium2")
    render_dir = tmp_path / "rendered"

    report = inspect_pdf(PDF_FIXTURE, render_dpi=144, render_dir=render_dir)

    rendered_page = report["pages"][0]["render"]
    assert rendered_page["file"] == "page-001.png"
    assert rendered_page["width_px"] == pytest.approx(1224, abs=2)
    assert rendered_page["height_px"] == pytest.approx(1584, abs=2)
    assert (render_dir / "page-001.png").is_file()
    assert report["rendering"]["dpi"] == 144


@pytest.mark.parametrize(
    (
        "pdf_fixture",
        "overlay_reference",
        "expected_orthographic_views",
        "expected_selected_image",
    ),
    [
        (RASTER_PDF_FIXTURE, RASTER_OVERLAY_REFERENCE, 2, 4),
        (RASTER_PDF_FIXTURE_1_2, RASTER_OVERLAY_REFERENCE_1_2, 1, 4),
        (
            RASTER_PDF_FIXTURE_1_20,
            RASTER_OVERLAY_REFERENCE_1_20,
            3,
            4,
        ),
    ],
    ids=["practice-problem-1-1", "practice-problem-1-2", "practice-problem-1-20"],
)
def test_raster_analysis_overlay_matches_practice_problem_reference(
    tmp_path,
    pdf_fixture,
    overlay_reference,
    expected_orthographic_views,
    expected_selected_image,
):
    _require_optional_reference_fixtures()
    if not Path(pdf_fixture).is_file() or not Path(overlay_reference).is_file():
        pytest.skip("Optional drawing reference fixtures are not included.")
    pytest.importorskip("pdfplumber")
    pytest.importorskip("pypdfium2")
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    analysis_dir = tmp_path / "analysis"

    report = inspect_pdf(
        pdf_fixture,
        analyze_raster=True,
        analysis_dir=analysis_dir,
    )

    raster_analysis = report["pages"][0]["raster_analysis"]
    assert raster_analysis["status"] == "ok"
    assert raster_analysis["orthographic_view_count"] == expected_orthographic_views
    assert raster_analysis["extraction"]["selected_image_index"] == expected_selected_image
    overlay_path = analysis_dir / "page-001-raster-overlay.png"
    assert overlay_path.is_file()
    actual = cv2.imread(str(overlay_path), cv2.IMREAD_COLOR)
    expected = cv2.imread(overlay_reference, cv2.IMREAD_COLOR)
    assert actual is not None
    assert expected is not None
    assert actual.shape == expected.shape
    np.testing.assert_array_equal(actual, expected)


def test_high_resolution_scanned_pdf_uses_scan_preprocessing(tmp_path):
    _require_optional_reference_fixtures()
    if not Path(SCANNED_PDF_FIXTURE).is_file() or not Path(
        SCANNED_OVERLAY_REFERENCE
    ).is_file():
        pytest.skip("Optional scanned drawing reference fixtures are not included.")
    pytest.importorskip("pdfplumber")
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    analysis_dir = tmp_path / "analysis"

    report = inspect_pdf(
        SCANNED_PDF_FIXTURE,
        analyze_raster=True,
        analysis_dir=analysis_dir,
    )

    raster_analysis = report["pages"][0]["raster_analysis"]
    assert raster_analysis["status"] == "ok"
    assert raster_analysis["scan_preprocessing_applied"] is True
    assert raster_analysis["source_size_px"] == {"width": 3805, "height": 2677}
    assert raster_analysis["analysis_scale"] < 1
    assert any("review them manually" in note for note in raster_analysis["limitations"])
    overlay_path = analysis_dir / "page-001-raster-overlay.png"
    actual = cv2.imread(str(overlay_path), cv2.IMREAD_COLOR)
    expected = cv2.imread(SCANNED_OVERLAY_REFERENCE, cv2.IMREAD_COLOR)
    assert actual is not None
    assert expected is not None
    assert actual.shape == expected.shape
    np.testing.assert_array_equal(actual, expected)


def test_raster_overlay_can_preserve_the_full_source_frame(tmp_path):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    from partbuilder.core.raster_drawing_analyzer import analyze_raster_drawing

    image = np.full((420, 680), 255, dtype=np.uint8)
    cv2.rectangle(image, (80, 60), (310, 260), 0, 3)
    output = tmp_path / "full-frame-overlay.png"

    report = analyze_raster_drawing(
        image,
        output_png=output,
        preserve_full_frame=True,
    )

    actual = cv2.imread(str(output), cv2.IMREAD_COLOR)
    assert report["overlay_preserves_full_frame"] is True
    assert actual is not None
    assert actual.shape[:2] == image.shape


def test_orthographic_view_image_is_padded_and_excludes_reference_views(tmp_path):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    from partbuilder.core.raster_drawing_analyzer import (
        _write_orthographic_views_png,
    )

    image = np.full((240, 500), 255, dtype=np.uint8)
    cv2.rectangle(image, (205, 85), (295, 155), 0, 3)
    cv2.line(image, (178, 120), (198, 120), 0, 2)
    cv2.rectangle(image, (420, 80), (470, 130), 0, 3)
    output = tmp_path / "orthographic-views.png"

    result = _write_orthographic_views_png(
        image,
        [
            {
                "view_id": "view-01",
                "classification": "orthographic",
                "bbox_px": {"x": 200, "y": 80, "width": 100, "height": 80},
            },
            {
                "view_id": "view-02",
                "classification": "isometric_reference",
                "bbox_px": {"x": 415, "y": 75, "width": 60, "height": 60},
            },
        ],
        {"left": 0, "top": 0},
        1.0,
        output,
    )

    actual = cv2.imread(str(output), cv2.IMREAD_GRAYSCALE)
    assert result is not None
    assert result["view_count"] == 1
    assert result["regions"][0]["bbox_source_px"] == {
        "x": 176,
        "y": 56,
        "width": 148,
        "height": 128,
    }
    assert actual is not None
    assert actual.shape == (176, 148)
    assert actual[24 + 64, 178 - 176] == 0
    assert result["regions"][0]["padding_ratio"] == 0.15


def test_near_circular_orthographic_candidate_is_separated_from_iso_reference(tmp_path):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    from partbuilder.core.raster_drawing_analyzer import analyze_raster_drawing

    image = np.full((420, 680), 255, dtype=np.uint8)
    cv2.rectangle(image, (0, 15), (679, 55), 0, -1)
    cv2.circle(image, (190, 210), 82, 0, 3)
    cv2.ellipse(image, (485, 210), (55, 90), 0, 0, 360, 180, -1)
    cv2.ellipse(image, (485, 210), (55, 90), 0, 0, 360, 0, 3)
    cv2.line(image, (450, 160), (500, 120), 0, 3)
    cv2.line(image, (500, 120), (540, 145), 0, 3)
    cv2.circle(image, (625, 85), 23, 0, -1)
    output = tmp_path / "orthographic-views.png"

    report = analyze_raster_drawing(image, orthographic_views_png=output)

    round_view = next(
        region for region in report["view_regions"]
        if region["bbox_px"]["x"] < 300 and region["bbox_px"]["y"] > 100
    )
    assert round_view["classification"] == "orthographic"
    assert "near-circular-projected-outline" in round_view["evidence"]
    assert any(
        "extreme-aspect-region" in region["evidence"]
        for region in report["view_regions"]
    )
    assert report["orthographic_extracted_view_count"] == 1
    assert report["orthographic_extraction"]["regions"][0]["view_id"] == (
        round_view["view_id"]
    )


def test_drawing_layout_preview_preserves_complete_source_image(tmp_path):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    from partbuilder.core.raster_drawing_analyzer import (
        _write_drawing_layout_png,
    )

    image = np.full((300, 400), 255, dtype=np.uint8)
    cv2.rectangle(image, (50, 50), (100, 90), 0, 2)
    cv2.rectangle(image, (260, 130), (330, 200), 0, 2)
    output = tmp_path / "drawing-view-layout.png"
    regions = [
        {
            "view_id": "front",
            "classification": "orthographic",
            "bbox_px": {"x": 50, "y": 50, "width": 60, "height": 50},
            "evidence": ["axis-aligned-line-dominance", "line_count:18"],
        },
        {
            "view_id": "isometric-candidate",
            "classification": "unknown",
            "bbox_px": {"x": 260, "y": 130, "width": 80, "height": 80},
            "evidence": ["weak-orientation-signal", "line_count:20"],
        },
        {
            "view_id": "title",
            "classification": "unknown",
            "bbox_px": {"x": 0, "y": 5, "width": 390, "height": 25},
            "evidence": ["extreme-aspect-region", "line_count:45"],
        },
    ]

    result = _write_drawing_layout_png(
        image,
        regions,
        {"left": 0, "top": 0},
        1.0,
        output,
    )

    actual = cv2.imread(str(output), cv2.IMREAD_GRAYSCALE)
    assert result is not None
    assert [region["view_id"] for region in result["regions"]] == [
        "front",
        "isometric-candidate",
        "title",
    ]
    assert result["layout"] == (
        "original source image preserved without rearrangement or crop"
    )
    assert result["source_image_preserved"] is True
    assert result["crop_bbox_source_px"] == {
        "x": 0,
        "y": 0,
        "width": 400,
        "height": 300,
    }
    assert actual is not None
    assert actual.shape == image.shape
    assert np.array_equal(actual, image)


def test_gui_raster_overlay_uses_complete_pdf_page_frame(tmp_path):
    _require_optional_reference_fixtures()
    if not Path(SCANNED_PDF_FIXTURE).is_file():
        pytest.skip("Optional scanned drawing reference fixture is not included.")
    pytest.importorskip("pdfplumber")
    pytest.importorskip("pypdfium2")
    cv2 = pytest.importorskip("cv2")

    report = inspect_pdf(
        RASTER_PDF_FIXTURE,
        render_dpi=120,
        render_dir=tmp_path,
        analyze_raster=True,
        analysis_dir=tmp_path,
        preserve_overlay_frame=True,
    )

    page = report["pages"][0]
    full_page = cv2.imread(str(tmp_path / page["render"]["file"]), cv2.IMREAD_COLOR)
    overlay_path = page["raster_analysis"]["full_page_overlay_png"]
    overlay = cv2.imread(overlay_path, cv2.IMREAD_COLOR)
    assert Path(overlay_path).name == "page-001-full-page-overlay.png"
    assert full_page is not None
    assert overlay is not None
    assert overlay.shape == full_page.shape
    assert page["raster_analysis"]["overlay_png"] != overlay_path
    extracted_path = page["raster_analysis"]["orthographic_views_png"]
    extracted = cv2.imread(extracted_path, cv2.IMREAD_COLOR)
    assert Path(extracted_path).name == "page-001-orthographic-views.png"
    assert extracted is not None
    assert page["raster_analysis"]["orthographic_extracted_view_count"] == (
        page["raster_analysis"]["orthographic_view_count"]
    )
    drawing_layout_path = page["raster_analysis"]["drawing_views_png"]
    drawing_layout = cv2.imread(drawing_layout_path, cv2.IMREAD_COLOR)
    assert Path(drawing_layout_path).name == "page-001-drawing-view-layout.png"
    assert drawing_layout is not None
    assert page["raster_analysis"]["drawing_view_layout"]["source_image_preserved"] is True


def test_content_type_covers_vector_raster_and_mixed_cases():
    from partbuilder.core.pdf_inspector import _content_type

    assert _content_type(True, False, False) == "vector"
    assert _content_type(False, True, False) == "raster"
    assert _content_type(True, True, True) == "mixed"
    assert _content_type(False, False, True) == "text-only"
    assert _content_type(False, False, False) == "empty"
