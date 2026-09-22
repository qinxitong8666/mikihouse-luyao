from __future__ import annotations

import hashlib
import math
import random
from pathlib import Path
from typing import Any

from PIL import Image
from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from .daily_quote import ALLOWED_CATEGORIES, CATEGORY_LABELS, DailyQuoteError


PAGE_WIDTH, PAGE_HEIGHT = A4
CARDS_PER_PAGE = 12
MARGIN_X = 18
MARGIN_Y = 20
GAP_X = 6
GAP_Y = 6
HEADER_H = 18
CARD_W = (PAGE_WIDTH - 2 * MARGIN_X - 2 * GAP_X) / 3
CARD_H = (PAGE_HEIGHT - 2 * MARGIN_Y - HEADER_H - 3 * GAP_Y) / 4
INK = colors.HexColor("#1D2530")
MUTED = colors.HexColor("#657080")
ACCENT = colors.HexColor("#CF1F2E")
LINE = colors.HexColor("#DDE2E8")
TINTS = {
    "footwear": colors.HexColor("#FFF8F3"),
    "baby": colors.HexColor("#F5FAFF"),
    "apparel": colors.HexColor("#FAF7FF"),
}


def _register_font() -> str:
    name = "DailyQuoteArialUnicode"
    try:
        pdfmetrics.getFont(name)
        return name
    except KeyError:
        pass
    candidates = (
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    )
    for path in candidates:
        if path.exists():
            pdfmetrics.registerFont(TTFont(name, str(path)))
            return name
    raise DailyQuoteError("no Unicode font is available for daily quote PDF")


def _wrap(text: str, font: str, size: float, width: float) -> list[str]:
    result: list[str] = []
    for logical in str(text).splitlines() or [""]:
        current = ""
        for char in logical:
            candidate = current + char
            if current and pdfmetrics.stringWidth(candidate, font, size) > width:
                result.append(current)
                current = char
            else:
                current = candidate
        result.append(current)
    return result


def _fit_lines(texts: list[str], font: str, width: float, height: float) -> tuple[float, list[str], float]:
    for size in (6.6, 6.2, 5.8, 5.4, 5.0, 4.6, 4.2):
        lines: list[str] = []
        for text in texts:
            lines.extend(_wrap(text, font, size, width))
        leading = size * 1.15
        if len(lines) * leading <= height:
            return size, lines, leading
    raise DailyQuoteError(f"variant text does not fit daily quote card: {texts[:3]}")


def _draw_image(canvas: Canvas, path: Path, x: float, y: float, width: float, height: float) -> None:
    with Image.open(path) as image:
        iw, ih = image.size
    scale = min(width / iw, height / ih)
    dw, dh = iw * scale, ih * scale
    canvas.drawImage(
        ImageReader(str(path)), x + (width - dw) / 2, y + (height - dh) / 2,
        dw, dh, preserveAspectRatio=True, mask=None,
    )


def _variant_lines(product: dict[str, Any]) -> list[str]:
    groups = product.get("variant_price_groups") or []
    if len(groups) == 1:
        return [
            f"{row['color']}: {'/'.join(row['sizes'])}"
            for row in groups[0].get("colors") or []
        ]
    lines: list[str] = []
    for group in groups:
        price = int(group["customer_price_cny"])
        for row in group.get("colors") or []:
            lines.append(f"{row['color']} {'/'.join(row['sizes'])} · ¥{price}")
    return lines


def _price_label(product: dict[str, Any]) -> str:
    groups = product.get("variant_price_groups") or []
    if len(groups) == 1:
        return f"人民币 ¥{int(groups[0]['customer_price_cny']):,}"
    prices = [int(group["customer_price_cny"]) for group in groups]
    return "人民币 " + "/".join(f"¥{value:,}" for value in prices)


def _draw_card(
    canvas: Canvas, product: dict[str, Any], image_path: Path, x: float, y: float, font: str
) -> None:
    canvas.setFillColor(TINTS[product["category"]])
    canvas.setStrokeColor(LINE)
    canvas.roundRect(x, y, CARD_W, CARD_H, 5, fill=1, stroke=1)
    pad = 7
    image_h = 72
    _draw_image(canvas, image_path, x + pad, y + CARD_H - pad - image_h, CARD_W - 2 * pad, image_h)

    text_x = x + pad
    text_w = CARD_W - 2 * pad
    name_top = y + CARD_H - pad - image_h - 4
    name_size = 7.5
    name_lines = _wrap(product["name"], font, name_size, text_w)
    while len(name_lines) > 2 and name_size > 5.5:
        name_size -= 0.4
        name_lines = _wrap(product["name"], font, name_size, text_w)
    if len(name_lines) > 2:
        raise DailyQuoteError(f"product name does not fit card: {product['product_number']}")
    canvas.setFillColor(INK)
    canvas.setFont(font, name_size)
    for index, line in enumerate(name_lines):
        canvas.drawString(text_x, name_top - name_size - index * name_size * 1.1, line)
    number_y = name_top - len(name_lines) * name_size * 1.1 - 9
    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica", 7.2)
    canvas.drawString(text_x, number_y, product["product_number"])

    price = _price_label(product)
    price_size = 9.2 if len(price) <= 24 else 7.2
    canvas.setFillColor(ACCENT)
    canvas.setFont(font, price_size)
    canvas.drawString(text_x, number_y - price_size - 3, price)
    variant_top = number_y - price_size - 8
    variant_bottom = y + 5
    size, lines, leading = _fit_lines(
        _variant_lines(product), font, text_w, max(8, variant_top - variant_bottom)
    )
    canvas.setFillColor(INK)
    canvas.setFont(font, size)
    for index, line in enumerate(lines):
        canvas.drawString(text_x, variant_top - size - index * leading, line)


def _chunk(values: list[Any], size: int) -> list[list[Any]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def _content_pages(products: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    pages: list[tuple[str, list[dict[str, Any]]]] = []
    for category in ALLOWED_CATEGORIES:
        rows = [row for row in products if row["category"] == category]
        pages.extend((category, batch) for batch in _chunk(rows, CARDS_PER_PAGE))
    return pages


def _index_page_count(product_count: int) -> int:
    return max(1, math.ceil(product_count / 168))


def generate_daily_quote_pdf(
    manifest: dict[str, Any],
    thumbnail_paths: dict[str, Path],
    output_path: Path,
    *,
    categories: tuple[str, ...] = ALLOWED_CATEGORIES,
) -> dict[str, Any]:
    products = [row for row in manifest["products"] if row["category"] in categories]
    if not products:
        raise DailyQuoteError("daily quote PDF requires at least one product")
    missing = sorted(row["product_number"] for row in products if row["product_number"] not in thumbnail_paths)
    if missing:
        raise DailyQuoteError(f"missing PDF thumbnails: {missing[:10]}")
    products_by_category = []
    for category in ALLOWED_CATEGORIES:
        if category in categories:
            products_by_category.extend(row for row in products if row["category"] == category)
    content_pages = _content_pages(products_by_category)
    content_pages = [row for row in content_pages if row[0] in categories]
    index_products = sorted(products_by_category, key=lambda row: row["product_number"])
    index_pages = _index_page_count(len(index_products))
    page_map: dict[str, int] = {}
    for page_offset, (_, rows) in enumerate(content_pages):
        physical = index_pages + page_offset + 1
        for row in rows:
            page_map[row["product_number"]] = physical

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas = Canvas(str(output_path), pagesize=A4, pageCompression=1, invariant=1)
    canvas.setTitle(f"MIKI HOUSE {manifest['quote_date']} 报价全集")
    canvas.setAuthor("MIKI HOUSE 每日报价生成工具")
    font = _register_font()

    for index_page, entries in enumerate(_chunk(index_products, 168), 1):
        canvas.bookmarkPage(f"index-{index_page}")
        if index_page == 1:
            canvas.addOutlineEntry("品番索引", "index-1", level=0, closed=False)
        canvas.setFillColor(INK)
        canvas.setFont(font, 13)
        canvas.drawString(MARGIN_X, PAGE_HEIGHT - 24, f"MIKI HOUSE {manifest['quote_date']} 报价品番索引")
        canvas.setFillColor(MUTED)
        canvas.setFont(font, 7)
        canvas.drawRightString(PAGE_WIDTH - MARGIN_X, PAGE_HEIGHT - 23, "点击品番可跳转商品页")
        col_w = (PAGE_WIDTH - 2 * MARGIN_X) / 3
        row_h = 13.3
        for idx, product in enumerate(entries):
            col, row = divmod(idx, 56)
            x = MARGIN_X + col * col_w
            y = PAGE_HEIGHT - 44 - row * row_h
            number = product["product_number"]
            label = f"{number}  ·  p.{page_map[number]}"
            canvas.setFont("Helvetica", 6.8)
            canvas.setFillColor(INK)
            canvas.drawString(x, y, label)
            canvas.linkAbsolute(number, f"product-{number}", (x, y - 2, x + col_w - 5, y + 8), thickness=0)
        canvas.showPage()

    seen_category: set[str] = set()
    for category, rows in content_pages:
        if category not in categories:
            continue
        if category not in seen_category:
            canvas.bookmarkPage(f"category-{category}")
            canvas.addOutlineEntry(CATEGORY_LABELS[category], f"category-{category}", level=0, closed=False)
            seen_category.add(category)
        canvas.setFont(font, 10)
        canvas.setFillColor(INK)
        canvas.drawString(MARGIN_X, PAGE_HEIGHT - MARGIN_Y + 2, f"【{CATEGORY_LABELS[category]}】")
        canvas.setFillColor(MUTED)
        canvas.setFont(font, 6.5)
        canvas.drawRightString(PAGE_WIDTH - MARGIN_X, PAGE_HEIGHT - MARGIN_Y + 2, "仅显示当前官网有货规格")
        for idx, product in enumerate(rows):
            row, col = divmod(idx, 3)
            x = MARGIN_X + col * (CARD_W + GAP_X)
            y = PAGE_HEIGHT - MARGIN_Y - HEADER_H - (row + 1) * CARD_H - row * GAP_Y
            canvas.bookmarkPage(f"product-{product['product_number']}")
            _draw_card(canvas, product, thumbnail_paths[product["product_number"]], x, y, font)
        canvas.showPage()
    canvas.save()
    return {
        "path": str(output_path),
        "file_size_bytes": output_path.stat().st_size,
        "sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
        "index_page_count": index_pages,
        "product_page_count": len(content_pages),
        "page_count": index_pages + len(content_pages),
        "product_count": len(products_by_category),
        "page_map": page_map,
        "categories": list(categories),
    }


def validate_daily_quote_pdf(
    pdf_path: Path,
    manifest: dict[str, Any],
    pdf_report: dict[str, Any],
    *,
    sample_size: int = 50,
) -> dict[str, Any]:
    reader = PdfReader(str(pdf_path))
    page_texts = [(page.extract_text() or "") for page in reader.pages]
    all_text = "\n".join(page_texts)
    products = [row["product_number"] for row in manifest["products"] if row["product_number"] in pdf_report["page_map"]]
    boundary = products[:3] + products[-3:]
    rng = random.Random(manifest["manifest_sha256"])
    target_sample_count = min(sample_size, len(products))
    samples = list(dict.fromkeys(boundary))
    remaining = [number for number in products if number not in samples]
    samples.extend(rng.sample(remaining, min(target_sample_count - len(samples), len(remaining))))
    checks: list[dict[str, Any]] = []
    for number in samples:
        expected_page = int(pdf_report["page_map"][number])
        pages = [idx + 1 for idx, text in enumerate(page_texts) if number in text]
        card_page_match = expected_page in pages
        exact_occurrences = all_text.count(number)
        checks.append({
            "product_number": number,
            "expected_card_page": expected_page,
            "found_pages": pages,
            "exact_occurrences": exact_occurrences,
            "passed": card_page_match and exact_occurrences >= 2,
        })
    forbidden = ["税入", "日元原价", "discount_rate", "jpy_to_cny_rate", "0.68", "×0.68", "定价公式"]
    forbidden_hits = [token for token in forbidden if token in all_text]
    outline_titles = []
    for item in reader.outline:
        title = getattr(item, "title", None)
        if title:
            outline_titles.append(str(title))
    required_outlines = [CATEGORY_LABELS[key] for key in ALLOWED_CATEGORIES if key in pdf_report["categories"]]
    passed = (
        len(reader.pages) == pdf_report["page_count"]
        and len(checks) >= min(50, len(products))
        and all(row["passed"] for row in checks)
        and not forbidden_hits
        and all(title in outline_titles for title in required_outlines)
    )
    result = {
        "status": "PASS" if passed else "FAIL",
        "page_count": len(reader.pages),
        "product_count": len(products),
        "search_sample_count": len(checks),
        "search_pass_count": sum(1 for row in checks if row["passed"]),
        "search_pass_rate": (sum(1 for row in checks if row["passed"]) / len(checks)) if checks else 0,
        "search_checks": checks,
        "forbidden_customer_text_hits": forbidden_hits,
        "outline_titles": outline_titles,
        "required_outline_titles": required_outlines,
        "text_layer_character_count": len(all_text),
    }
    if not passed:
        raise DailyQuoteError(f"daily quote PDF validation failed: {result}")
    return result
