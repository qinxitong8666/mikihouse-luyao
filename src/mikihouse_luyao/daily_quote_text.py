from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .daily_quote import ALLOWED_CATEGORIES, CATEGORY_LABELS


def _color_sizes(group: dict[str, Any]) -> str:
    return "；".join(
        f"{row['color']}:{'/'.join(row['sizes'])}" for row in group.get("colors") or []
    )


def format_product_quote(product: dict[str, Any], *, include_product_name: bool = False) -> list[str]:
    prefix = product["product_number"]
    if include_product_name:
        prefix += f" {product['name']}"
    groups = product.get("variant_price_groups") or []
    if len(groups) == 1:
        group = groups[0]
        return [f"{prefix}｜{group['customer_price_cny']}元｜{_color_sizes(group)}"]
    lines = [prefix]
    for group in groups:
        for color in group.get("colors") or []:
            lines.append(
                f"{color['color']} {'/'.join(color['sizes'])}｜{group['customer_price_cny']}元"
            )
    return lines


def render_text_quote(manifest: dict[str, Any], *, include_product_name: bool = False) -> str:
    lines = ["MIKI HOUSE 日本官网当日报价", f"更新：{manifest['quote_date']}", "仅显示当前官网有货规格", ""]
    for category in ALLOWED_CATEGORIES:
        lines.append(f"【{CATEGORY_LABELS[category]}】")
        products = [row for row in manifest["products"] if row["category"] == category]
        for product in products:
            lines.extend(format_product_quote(product, include_product_name=include_product_name))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def text_stats(text: str, product_count: int) -> dict[str, int]:
    lines = text.splitlines()
    return {
        "unicode_character_count": len(text),
        "utf8_byte_count": len(text.encode("utf-8")),
        "line_count": len(lines),
        "product_count": product_count,
        "longest_line_character_count": max((len(line) for line in lines), default=0),
    }


def build_favorite_payloads(
    manifest: dict[str, Any],
    *,
    text_quote: str,
    full_pdf: Path,
    category_pdfs: dict[str, Path] | None,
    max_pdf_mb: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    quote_date = manifest["quote_date"]
    year, month, day = (int(value) for value in quote_date.split("-"))
    title_base = f"MIKI HOUSE {month}月{day}日报价"
    full_size = full_pdf.stat().st_size
    threshold = max_pdf_mb * 1024 * 1024
    if full_size <= threshold:
        attachments = [str(full_pdf)]
        attachment_strategy = "FULL_CATALOG"
    else:
        if not category_pdfs or set(category_pdfs) != set(ALLOWED_CATEGORIES):
            raise ValueError("oversized full PDF requires all three category PDFs")
        attachments = [str(category_pdfs[key]) for key in ALLOWED_CATEGORIES]
        attachment_strategy = "THREE_CATEGORY_PDFS"
    body = "\n".join([
        "MIKI HOUSE 日本官网当日报价",
        f"更新：{year}年{month}月{day}日",
        f"当日可报价：{manifest['counts']['included_in_stock_product_count']}款",
        "仅显示当前官网有货规格",
        "库存随官网变化，下单前请再次确认",
    ])
    common = {
        "schema_version": 1,
        "mode": "PREVIEW_ONLY",
        "real_wechat_write_enabled": False,
        "source_manifest_sha256": manifest["manifest_sha256"],
        "quote_date": quote_date,
    }
    pdf_payload = {
        **common,
        "favorite_kind": "PDF",
        "title": f"{title_base}｜PDF版",
        "body": body,
        "attachments": attachments,
        "attachment_strategy": attachment_strategy,
        "full_pdf_size_bytes": full_size,
    }
    text_payload = {
        **common,
        "favorite_kind": "TEXT",
        "title": f"{title_base}｜文字版",
        "body": text_quote,
        "attachments": [],
        "capacity_readiness": "REAL_WECHAT_TEXT_CAPACITY_NOT_YET_VERIFIED",
        "reference_observation": {
            "repository": "qinxitong8666/luyao-quote-assistant",
            "commit": "527e7e38cf7bd76c8f29a8701af96e4f76d5ae7d",
            "observed_chunked_note_characters": 27893,
            "hard_capacity_contract_available": False,
        },
    }
    return pdf_payload, text_payload


def render_favorite_preview(payload: dict[str, Any]) -> str:
    lines = [payload["title"], "", payload["body"]]
    if payload.get("attachments"):
        lines.extend(["", "附件：", *[f"- {Path(path).name}" for path in payload["attachments"]]])
    lines.extend(["", "状态：仅本地预览，未写入微信收藏。"])
    return "\n".join(lines).rstrip() + "\n"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
