from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Dict, Union

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
FULL_CARD_WIDTH = PAGE_WIDTH - 2 * MARGIN
ACCENT = colors.HexColor("#D71920")
INK = colors.HexColor("#20242A")
MUTED = colors.HexColor("#656C76")
LIGHT_LINE = colors.HexColor("#E3E5E8")
SHOE_TINT = colors.HexColor("#FAF7F3")
ImageSet = Union[Path, Dict[str, Path]]


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
    # ImageReader keeps JPEG source data at its original pixel dimensions; only
    # the PDF display matrix scales it, so zooming does not use a generated thumbnail.
    canvas.drawImage(
        ImageReader(str(path)),
        x + (width - draw_width) / 2,
        y + (height - draw_height) / 2,
        draw_width,
        draw_height,
        preserveAspectRatio=True,
        mask="auto",
    )


def _ordered_colors(product: Product) -> list[str]:
    return list(dict.fromkeys(variant.color or "-" for variant in product.variants))


def _format_cm(value: int) -> str:
    return str(value // 2) if value % 2 == 0 else f"{value // 2}.5"


def format_available_sizes(sizes: list[str]) -> str:
    unique = list(dict.fromkeys(size for size in sizes if size))
    if not unique:
        return "暂无可售尺码"
    halves: list[int] = []
    for size in unique:
        match = re.fullmatch(r"(\d+)(?:\.(0|5))?cm", size)
        if not match:
            return "/".join(unique)
        halves.append(int(match.group(1)) * 2 + (1 if match.group(2) == "5" else 0))
    ordered = sorted(set(halves))
    if len(ordered) >= 2 and all(right - left == 1 for left, right in zip(ordered, ordered[1:])):
        return f"{_format_cm(ordered[0])}-{_format_cm(ordered[-1])}cm"
    return "/".join(_format_cm(value) for value in ordered) + "cm"


def _available_prices(product: Product) -> list[int]:
    return sorted({variant.pdf_price for variant in product.variants if variant.in_stock})


def _price_text(product: Product) -> str:
    prices = _available_prices(product)
    if not prices:
        return "暂时缺货"
    if len(prices) == 1:
        return f"人民币 ¥{prices[0]:,}"
    return f"人民币 ¥{prices[0]:,} - ¥{prices[-1]:,}"


def _variant_text(product: Product) -> tuple[str, list[int]]:
    available = [variant for variant in product.variants if variant.in_stock]
    if not available:
        return "暂无可售颜色 / 尺码", []
    prices = sorted({variant.pdf_price for variant in available})
    grouped: dict[tuple[str, int], list[str]] = {}
    for variant in available:
        key = (variant.color or "-", variant.pdf_price)
        sizes = grouped.setdefault(key, [])
        if variant.size and variant.size not in sizes:
            sizes.append(variant.size)
    rows = []
    for (color, price), sizes in grouped.items():
        row = f"{color}: {format_available_sizes(sizes)}"
        if len(prices) > 1:
            row += f"  ¥{price:,}"
        rows.append(row)
    return "\n".join(rows), prices


def _shoe_detail_lines(product: Product) -> list[str]:
    global_prices = _available_prices(product)
    lines: list[str] = []
    for color in _ordered_colors(product):
        available = [v for v in product.variants if (v.color or "-") == color and v.in_stock]
        if not available:
            lines.append(f"{color}: 暂无可售尺码")
            continue
        by_price: dict[int, list[str]] = {}
        for variant in available:
            by_price.setdefault(variant.pdf_price, []).append(variant.size)
        for index, (price, sizes) in enumerate(by_price.items()):
            prefix = f"{color}: " if index == 0 else "  "
            line = prefix + format_available_sizes(sizes)
            if len(global_prices) > 1:
                line += f"  ¥{price:,}"
            lines.append(line)
    return lines


def _draw_regular_card(canvas: Canvas, product: Product, image_path: Path, x: float, y: float, font: str) -> None:
    canvas.setFillColor(colors.white)
    canvas.setStrokeColor(LIGHT_LINE)
    canvas.roundRect(x, y, CARD_WIDTH, CARD_HEIGHT, 10, fill=1, stroke=1)
    image_padding = 14
    image_height = CARD_HEIGHT - 165
    _draw_contained_image(canvas, image_path, x + image_padding, y + 154, CARD_WIDTH - 2 * image_padding, image_height)

    text_x = x + 16
    max_width = CARD_WIDTH - 32
    canvas.setFillColor(INK)
    name_size, name_lines = _fit_wrapped_text(product.name, font, max_width, 3, 13, 8)
    _draw_lines(canvas, name_lines, text_x, y + 148, font, name_size, name_size * 1.18)

    canvas.setFillColor(MUTED)
    canvas.setFont(font, 9.5)
    canvas.drawString(text_x, y + 96, f"品番  {product.product_number}")
    variant_text, _ = _variant_text(product)
    variant_size, variant_lines = _fit_wrapped_text(variant_text, font, max_width, 7, 9.5, 6.5)
    _draw_lines(canvas, variant_lines, text_x, y + 88, font, variant_size, variant_size * 1.13)

    canvas.setFillColor(ACCENT)
    price_size, price_lines = _fit_wrapped_text(_price_text(product), font, max_width, 1, 18, 11)
    canvas.setFont(font, price_size)
    canvas.drawRightString(x + CARD_WIDTH - 16, y + 17, price_lines[0])


def _draw_shoe_collage(
    canvas: Canvas,
    product: Product,
    images: dict[str, Path],
    x: float,
    y: float,
    width: float,
    height: float,
    font: str,
    wide: bool,
) -> None:
    colors_in_order = _ordered_colors(product)
    count = len(colors_in_order)
    if count == 1:
        columns, rows = 1, 1
    elif count == 2:
        columns, rows = 2, 1
    elif count <= 4:
        columns, rows = 2, 2
    elif count <= 6:
        columns, rows = 3, 2
    else:
        rows = 2 if count <= 12 else math.ceil(count / 6)
        columns = math.ceil(count / rows)
    gap = 5 if wide else 4
    cell_width = (width - gap * (columns - 1)) / columns
    cell_height = (height - gap * (rows - 1)) / rows
    label_height = 11
    for index, color in enumerate(colors_in_order):
        if color not in images:
            raise ValueError(f"missing cached footwear color image: {product.product_number} / {color}")
        row, column = divmod(index, columns)
        cell_x = x + column * (cell_width + gap)
        cell_y = y + height - (row + 1) * cell_height - row * gap
        _draw_contained_image(canvas, images[color], cell_x, cell_y + label_height, cell_width, cell_height - label_height)
        label_size, label_lines = _fit_wrapped_text(color, font, cell_width, 1, 7.5 if wide else 7, 5)
        canvas.setFillColor(MUTED)
        canvas.setFont(font, label_size)
        label_width = pdfmetrics.stringWidth(label_lines[0], font, label_size)
        canvas.drawString(cell_x + (cell_width - label_width) / 2, cell_y + 1.5, label_lines[0])


def _draw_shoe_card(
    canvas: Canvas,
    product: Product,
    images: dict[str, Path],
    x: float,
    y: float,
    width: float,
    font: str,
) -> None:
    wide = width > CARD_WIDTH + 1
    canvas.setFillColor(SHOE_TINT)
    canvas.setStrokeColor(colors.HexColor("#D9D1C8"))
    canvas.roundRect(x, y, width, CARD_HEIGHT, 10, fill=1, stroke=1)
    text_x = x + 14
    text_width = width - 28

    canvas.setFillColor(INK)
    name_size, name_lines = _fit_wrapped_text(product.name, font, text_width, 2, 11.5 if wide else 10.5, 7)
    _draw_lines(canvas, name_lines, text_x, y + CARD_HEIGHT - 9, font, name_size, name_size * 1.12)
    header_bottom = y + CARD_HEIGHT - 12 - len(name_lines) * name_size * 1.12
    canvas.setFillColor(MUTED)
    canvas.setFont(font, 8.5)
    canvas.drawString(text_x, header_bottom - 8, f"品番  {product.product_number}")

    collage_y = y + (117 if wide else 128)
    collage_top = header_bottom - 13
    _draw_shoe_collage(
        canvas, product, images, text_x, collage_y, text_width, max(40, collage_top - collage_y), font, wide
    )

    detail_lines = _shoe_detail_lines(product)
    canvas.setFillColor(INK)
    if wide:
        split = math.ceil(len(detail_lines) / 2)
        columns = (detail_lines[:split], detail_lines[split:])
        column_gap = 18
        column_width = (text_width - column_gap) / 2
        for index, lines in enumerate(columns):
            if not lines:
                continue
            detail_size, wrapped = _fit_wrapped_text(
                "\n".join(lines), font, column_width, 12, 8.2, 5.5
            )
            _draw_lines(
                canvas, wrapped, text_x + index * (column_width + column_gap), y + 109,
                font, detail_size, detail_size * 1.12,
            )
    else:
        detail_size, wrapped = _fit_wrapped_text(
            "\n".join(detail_lines), font, text_width, 10, 8.2, 5.5
        )
        _draw_lines(canvas, wrapped, text_x, y + 120, font, detail_size, detail_size * 1.1)

    canvas.setFillColor(ACCENT)
    price_size, price_lines = _fit_wrapped_text(_price_text(product), font, text_width, 1, 17, 10)
    canvas.setFont(font, price_size)
    canvas.drawRightString(x + width - 14, y + 15, price_lines[0])


def _needs_half_page(product: Product) -> bool:
    return product.is_footwear and len(_ordered_colors(product)) > 6


def catalog_page_count_from_data(products: list[dict]) -> int:
    occupied = 0
    for product in sorted(products, key=lambda item: not bool(item.get("is_footwear"))):
        colors = list(dict.fromkeys((variant.get("color") or "-") for variant in product.get("variants", [])))
        span = 2 if product.get("is_footwear") and len(colors) > 6 else 1
        cell = occupied % 4
        if span == 2 and cell % 2:
            occupied += 1
            cell = occupied % 4
        if span == 2 and cell > 2:
            occupied += 4 - cell
        occupied += span
    return math.ceil(occupied / 4)


def _main_image(images: ImageSet, product: Product) -> Path:
    if isinstance(images, Path):
        return images
    if not images:
        raise ValueError(f"missing cached images: {product.product_number}")
    return next(iter(images.values()))


def generate_price_list(
    products: list[Product],
    image_paths: dict[str, ImageSet],
    output_path: str | Path,
) -> Path:
    if not products:
        raise ValueError("at least one product is required")
    missing = [p.product_number for p in products if p.product_number not in image_paths]
    if missing:
        raise ValueError(f"missing cached images: {', '.join(missing)}")
    ordered_products = sorted(products, key=lambda product: not product.is_footwear)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    font = _register_font()
    canvas = Canvas(str(path), pagesize=A4, pageCompression=1)
    canvas.setTitle("MIKI HOUSE 商品册")
    canvas.setAuthor("")
    quarter_positions = (
        (MARGIN, PAGE_HEIGHT - MARGIN - CARD_HEIGHT),
        (MARGIN + CARD_WIDTH + GAP, PAGE_HEIGHT - MARGIN - CARD_HEIGHT),
        (MARGIN, MARGIN),
        (MARGIN + CARD_WIDTH + GAP, MARGIN),
    )
    cell = 0
    for product in ordered_products:
        half_page = _needs_half_page(product)
        if half_page and cell % 2:
            cell += 1
        if cell >= 4:
            canvas.showPage()
            cell = 0
        if half_page:
            row = cell // 2
            y = quarter_positions[row * 2][1]
            images = image_paths[product.product_number]
            if not isinstance(images, dict):
                raise ValueError(f"footwear requires color images: {product.product_number}")
            _draw_shoe_card(canvas, product, images, MARGIN, y, FULL_CARD_WIDTH, font)
            cell += 2
        else:
            x, y = quarter_positions[cell]
            images = image_paths[product.product_number]
            if product.is_footwear:
                if not isinstance(images, dict):
                    raise ValueError(f"footwear requires color images: {product.product_number}")
                _draw_shoe_card(canvas, product, images, x, y, CARD_WIDTH, font)
            else:
                _draw_regular_card(canvas, product, _main_image(images, product), x, y, font)
            cell += 1
    canvas.save()
    return path
