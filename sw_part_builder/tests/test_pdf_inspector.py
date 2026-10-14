import os

import pytest

from partbuilder.core.pdf_inspector import PdfInspectionError, inspect_pdf


PDF_FIXTURE = os.path.join(os.path.dirname(__file__), "alia_test_blueprint.pdf")


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


def test_content_type_covers_vector_raster_and_mixed_cases():
    from partbuilder.core.pdf_inspector import _content_type

    assert _content_type(True, False, False) == "vector"
    assert _content_type(False, True, False) == "raster"
    assert _content_type(True, True, True) == "mixed"
    assert _content_type(False, False, True) == "text-only"
    assert _content_type(False, False, False) == "empty"
