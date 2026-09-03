from __future__ import annotations

from pathlib import Path

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from .models import Product


PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN = 28
GAP = 18
CARD_WIDTH = (PAGE_WIDTH - 2 * MARGIN - GAP) / 2
CARD_HEIGHT = (PAGE_HEIGHT - 2 * MARGIN - GAP) / 2
ACCENT = colors.HexColor("#D71920")
INK = colors.HexColor("#20242A")
MUTED = colors.HexColor("#656C76")


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


def _fit_text(text: str, font: str, max_width: float, start_size: float, minimum: float = 8) -> float:
    size = start_size
    while size > minimum and pdfmetrics.stringWidth(text, font, size) > max_width:
        size -= 0.5
    return size


def _draw_contained_image(canvas: Canvas, path: Path, x: float, y: float, width: float, height: float) -> None:
    with PILImage.open(path) as image:
        image_width, image_height = image.size
    scale = min(width / image_width, height / image_height)
    draw_width, draw_height = image_width * scale, image_height * scale
    canvas.drawImage(
        ImageReader(str(path)),
        x + (width - draw_width) / 2,
        y + (height - draw_height) / 2,
        draw_width,
        draw_height,
        preserveAspectRatio=True,
        mask="auto",
    )


def _color_size_text(product: Product) -> str:
    grouped: dict[str, list[str]] = {}
    for variant in product.variants:
        if variant.in_stock:
            grouped.setdefault(variant.color or "-", []).append(variant.size or "-")
    if not grouped:
        return "暂无可售尺码"
    return "  |  ".join(f"{color}: {' / '.join(sizes)}" for color, sizes in grouped.items())


def _draw_card(canvas: Canvas, product: Product, image_path: Path, x: float, y: float, font: str) -> None:
    canvas.setFillColor(colors.white)
    canvas.setStrokeColor(colors.HexColor("#E3E5E8"))
    canvas.roundRect(x, y, CARD_WIDTH, CARD_HEIGHT, 10, fill=1, stroke=1)
    image_padding = 14
    image_height = CARD_HEIGHT - 116
    _draw_contained_image(canvas, image_path, x + image_padding, y + 102, CARD_WIDTH - 2 * image_padding, image_height)

    text_x = x + 16
    max_width = CARD_WIDTH - 32
    canvas.setFillColor(INK)
    name_size = _fit_text(product.name, font, max_width, 13, 9)
    canvas.setFont(font, name_size)
    canvas.drawString(text_x, y + 82, product.name)

    canvas.setFillColor(MUTED)
    canvas.setFont(font, 9.5)
    canvas.drawString(text_x, y + 63, f"品番  {product.product_number}")
    variants = _color_size_text(product)
    variant_size = _fit_text(variants, font, max_width, 9.5, 7.5)
    canvas.setFont(font, variant_size)
    canvas.drawString(text_x, y + 45, variants)

    canvas.setFillColor(ACCENT)
    price = f"人民币 ¥{product.pdf_price:,}"
    price_size = _fit_text(price, font, max_width, 18, 13)
    canvas.setFont(font, price_size)
    canvas.drawRightString(x + CARD_WIDTH - 16, y + 18, price)


def generate_price_list(
    products: list[Product],
    image_paths: dict[str, Path],
    output_path: str | Path,
) -> Path:
    if not products:
        raise ValueError("at least one product is required")
    missing = [p.product_number for p in products if p.product_number not in image_paths]
    if missing:
        raise ValueError(f"missing cached images: {', '.join(missing)}")
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    font = _register_font()
    canvas = Canvas(str(path), pagesize=A4, pageCompression=1)
    canvas.setTitle("MIKI HOUSE 商品册")
    canvas.setAuthor("")
    positions = (
        (MARGIN, PAGE_HEIGHT - MARGIN - CARD_HEIGHT),
        (MARGIN + CARD_WIDTH + GAP, PAGE_HEIGHT - MARGIN - CARD_HEIGHT),
        (MARGIN, MARGIN),
        (MARGIN + CARD_WIDTH + GAP, MARGIN),
    )
    for index, product in enumerate(products):
        if index and index % 4 == 0:
            canvas.showPage()
        x, y = positions[index % 4]
        _draw_card(canvas, product, image_paths[product.product_number], x, y, font)
    canvas.save()
    return path
