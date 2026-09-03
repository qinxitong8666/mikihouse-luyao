from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from pypdf import PdfReader
from reportlab.pdfgen.canvas import Canvas

from scripts.final_visual_qa import validate_pdf_contents, validate_rendered_pages


def test_validate_rendered_pages_requires_complete_nonblank_200dpi_set(tmp_path: Path) -> None:
    image = Image.new("RGB", (1654, 2339), "white")
    ImageDraw.Draw(image).rectangle((100, 100, 300, 300), fill="black")
    image.save(tmp_path / "page-01.png")

    assert validate_rendered_pages(tmp_path, 1, 200, 595.276, 841.89) == (1654, 2339)
    with pytest.raises(RuntimeError, match="rendered page mismatch"):
        validate_rendered_pages(tmp_path, 2, 200, 595.276, 841.89)


def test_validate_pdf_contents_rejects_failed_sku(tmp_path: Path) -> None:
    fixture = tmp_path / "catalog.pdf"
    canvas = Canvas(str(fixture))
    canvas.drawString(72, 720, "10-1105-495")
    canvas.save()

    with pytest.raises(RuntimeError, match="failed SKUs leaked"):
        validate_pdf_contents(PdfReader(fixture), [], [{"product_number": "10-1105-495"}])
