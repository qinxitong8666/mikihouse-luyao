from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .models import Product


def _register_font() -> str:
    candidates = (
        ("ArialUnicode", "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
        ("NotoSansCJK", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    )
    for name, path in candidates:
        if Path(path).is_file():
            try:
                pdfmetrics.getFont(name)
            except KeyError:
                pdfmetrics.registerFont(TTFont(name, path))
            return name
    name = "HeiseiKakuGo-W5"
    try:
        pdfmetrics.getFont(name)
    except KeyError:
        pdfmetrics.registerFont(UnicodeCIDFont(name))
    return name


def generate_price_list(products: list[Product], output_path: str | Path) -> Path:
    if not products:
        raise ValueError("at least one product is required")
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    font = _register_font()
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=16 * mm, bottomMargin=16 * mm)
    styles = getSampleStyleSheet()
    for style in styles.byName.values():
        style.fontName = font
    story = [Paragraph("MIKI HOUSE 価格表", styles["Title"]), Spacer(1, 6 * mm)]
    rows = [["商品番号", "商品名", "税込価格 (JPY)", "PDF售价", "カラー / サイズ / 在庫"]]
    for product in products:
        variants = "<br/>".join(f"{v.color} / {v.size} / {v.stock_text}" for v in product.variants)
        rows.append([
            product.product_number,
            Paragraph(product.name, styles["BodyText"]),
            f"¥{product.tax_included_price_jpy:,}",
            f"{product.pdf_price:,}",
            Paragraph(variants, styles["BodyText"]),
        ])
    table = Table(rows, colWidths=[29 * mm, 48 * mm, 28 * mm, 23 * mm, 50 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#D71920")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#C8CDD2")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.extend([table, Spacer(1, 5 * mm), Paragraph("定价公式: ceil(税込価格 × 0.73 × 0.0435)", styles["BodyText"])])
    doc.build(story)
    return path
