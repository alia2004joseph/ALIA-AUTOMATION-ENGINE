"""Create a simple vector PDF for testing blueprint ingestion.

Run from the project root with:
    python tests/create_vector_test_pdf.py
"""
from pathlib import Path

try:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
except ImportError as exc:
    raise SystemExit(
        "ReportLab is required to create the test PDF. Install it with: "
        "python -m pip install reportlab"
    ) from exc

MM_TO_POINTS = 72.0 / 25.4


def make_test_pdf(filename=None):
    output_path = Path(filename) if filename else Path(__file__).with_name(
        "alia_test_blueprint.pdf"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    pdf = canvas.Canvas(str(output_path), pagesize=letter)
    pdf.setStrokeColorRGB(0, 0, 0)
    pdf.setLineWidth(1)
    pdf.rect(
        100,
        300,
        120 * MM_TO_POINTS,
        80 * MM_TO_POINTS,
    )
    pdf.save()
    print(f"Created vector test drawing: {output_path.resolve()}")
    return output_path


if __name__ == "__main__":
    make_test_pdf()
