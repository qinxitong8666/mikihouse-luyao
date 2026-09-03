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


def _wrap_text(text: str, font: str, size: float, max_width: float) -> list[str]:
    lines: list[str] = []
    current = ""
    for character in text:
        if character == "\n":
            lines.append(current.rstrip())
            current = ""
            continue
        candidate = current + character
        if current and pdfmetrics.stringWidth(candidate, font, size) > max_width:
            lines.append(current.rstrip())
            current = character.lstrip()
        else:
            current = candidate
    if current or not lines:
        lines.append(current.rstrip())
    return lines


def _fit_wrapped_text(
    text: str,
    font: str,
    max_width: float,
    max_lines: int,
    start_size: float,
    minimum: float,
) -> tuple[float, list[str]]:
    size = start_size
    lines = _wrap_text(text, font, size, max_width)
    while len(lines) > max_lines and size > minimum:
        size = max(minimum, size - 0.5)
        lines = _wrap_text(text, font, size, max_width)
    if len(lines) > max_lines:
        raise ValueError(f"text does not fit in product card: {text}")
    return size, lines


def _draw_lines(canvas: Canvas, lines: list[str], x: float, top: float, font: str, size: float, leading: float) -> None:
    canvas.setFont(font, size)
    for index, line in enumerate(lines):
        canvas.drawString(x, top - size - index * leading, line)


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


def _variant_text(product: Product) -> tuple[str, list[int]]:
    available = [variant for variant in product.variants if variant.in_stock]
    if not available:
        return "暂无可售颜色 / 尺码", []
    prices = sorted({variant.pdf_price for variant in available})
    grouped: dict[tuple[str, int], list[str]] = {}
    for variant in available:
        key = (variant.color or "-", variant.pdf_price)
        sizes = grouped.setdefault(key, [])
        size = variant.size or "-"
        if size not in sizes:
            sizes.append(size)
    combined: dict[tuple[tuple[str, ...], int], list[str]] = {}
    for (color, price), sizes in grouped.items():
        combined.setdefault((tuple(sizes), price), []).append(color)

    rows = []
    for (sizes, price), colors_for_sizes in combined.items():
        if sizes and all(size.endswith("cm") for size in sizes):
            size_text = "/".join(size.removesuffix("cm") for size in sizes) + "cm"
        else:
            size_text = "/".join(sizes)
        row = f"{' / '.join(colors_for_sizes)}: {size_text}"
        if len(prices) > 1:
            row += f"  ¥{price:,}"
        rows.append(row)
    return "\n".join(rows), prices


def _draw_card(canvas: Canvas, product: Product, image_path: Path, x: float, y: float, font: str) -> None:
    canvas.setFillColor(colors.white)
    canvas.setStrokeColor(colors.HexColor("#E3E5E8"))
    canvas.roundRect(x, y, CARD_WIDTH, CARD_HEIGHT, 10, fill=1, stroke=1)
    image_padding = 14
    image_height = CARD_HEIGHT - 165
    _draw_contained_image(canvas, image_path, x + image_padding, y + 154, CARD_WIDTH - 2 * image_padding, image_height)

    text_x = x + 16
    max_width = CARD_WIDTH - 32
    canvas.setFillColor(INK)
    name_size, name_lines = _fit_wrapped_text(product.name, font, max_width, 3, 13, 8)
    while (
        len(name_lines) > 1
        and pdfmetrics.stringWidth(name_lines[-1], font, name_size) < max_width * 0.2
        and name_size > 8
    ):
        name_size = max(8, name_size - 0.5)
        name_lines = _wrap_text(product.name, font, name_size, max_width)
    _draw_lines(canvas, name_lines, text_x, y + 148, font, name_size, name_size * 1.18)

    canvas.setFillColor(MUTED)
    canvas.setFont(font, 9.5)
    canvas.drawString(text_x, y + 96, f"品番  {product.product_number}")
    variant_text, prices = _variant_text(product)
    variant_size, variant_lines = _fit_wrapped_text(variant_text, font, max_width, 7, 9.5, 6.5)
    _draw_lines(canvas, variant_lines, text_x, y + 88, font, variant_size, variant_size * 1.13)

    canvas.setFillColor(ACCENT)
    if not prices:
        price = "暂时缺货"
    elif len(prices) == 1:
        price = f"人民币 ¥{prices[0]:,}"
    else:
        price = f"人民币 ¥{prices[0]:,} - ¥{prices[-1]:,}"
    price_size, price_lines = _fit_wrapped_text(price, font, max_width, 1, 18, 11)
    canvas.setFont(font, price_size)
    canvas.drawRightString(x + CARD_WIDTH - 16, y + 17, price_lines[0])


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
