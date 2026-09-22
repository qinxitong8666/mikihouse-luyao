from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader

from mikihouse_luyao.daily_quote import (
    ALL_VARIANTS_UNAVAILABLE,
    DAILY_QUOTE_ELIGIBLE,
    LIMITED_TIME_PRICE,
    NO_DISCOUNT_LIST,
    NON_SELLABLE_SERVICE_OR_ADDON,
    NON_WHITELIST_CATEGORY,
    WEB_EXCLUSIVE,
    assess_daily_quote_product,
    build_daily_quote_manifest,
    calculate_customer_price_cny,
    group_available_variants,
)
from mikihouse_luyao.daily_quote_fx import (
    FxError,
    FrozenFxRate,
    assert_fx_fresh,
    fetch_ecb_reference_rate,
    manual_fx_rate,
    parse_ecb_reference_rates,
)
from mikihouse_luyao.daily_quote_images import create_thumbnail
from mikihouse_luyao.daily_quote_pdf import generate_daily_quote_pdf, validate_daily_quote_pdf
from mikihouse_luyao.daily_quote_runner import DailyQuoteRunError, _validate_crawl
from mikihouse_luyao.daily_quote_text import (
    build_favorite_payloads,
    render_compact_text_quote,
    render_text_quote,
    text_stats,
)


def special_set(included: str = "10-1105-495") -> set[str]:
    values = {included}
    index = 0
    while len(values) < 351:
        values.add(f"99-{index:04d}-999")
        index += 1
    return values


def product(
    number: str,
    name: str,
    *,
    tags: list[str] | None = None,
    variants: list[dict] | None = None,
    description: str = "",
) -> dict:
    variants = variants or [{
        "sku": f"{number.replace('-', '')}001",
        "color": "赤",
        "size": "13cm",
        "available_for_sale": True,
        "tax_included_price_jpy": 16500,
        "compare_at_price_jpy": 16500,
    }]
    image = {"url": f"https://cdn.shopify.com/{number}.jpg", "width": 800, "height": 800}
    variants = [
        {
            **row,
            "resolved_image": row.get("resolved_image") or image,
            "variant_image": row.get("variant_image") or image,
        }
        for row in variants
    ]
    return {
        "product_number": number,
        "handle": number,
        "name": name,
        "product_type": "通常商品",
        "tags": tags or [],
        "description": description,
        "description_html": f"<p>{description}</p>",
        "main_image": image,
        "ordered_images": [{"order": 1, "role": "main", "image": {"url": f"https://cdn.shopify.com/{number}.jpg"}}],
        "product_url": f"https://www.mikihouse.co.jp/products/{number}",
        "variants": variants,
    }


def frozen_fx(rate: str = "0.048") -> FrozenFxRate:
    return FrozenFxRate(
        provider="TEST",
        rate_date="2026-09-18",
        jpy_to_cny_rate=Decimal(rate),
        fetched_at="2026-09-18T12:00:00+00:00",
        raw_response_sha256="a" * 64,
        source_url="https://example.test/fx",
    )


def test_decimal_price_uses_ceiling_without_float() -> None:
    assert calculate_customer_price_cny(16500, Decimal("0.68"), Decimal("0.048")) == 539
    assert calculate_customer_price_cny(1, Decimal("0.68"), Decimal("0.048")) == 1


def test_daily_pool_exclusions_and_whitelist_are_fail_closed() -> None:
    specials = special_set()
    assert assess_daily_quote_product(product("10-1105-495", "シャツ"), specials)[1] == NO_DISCOUNT_LIST
    assert assess_daily_quote_product(product("10-0001-001", "【WEB限定】シャツ"), specials)[1] == WEB_EXCLUSIVE
    promo = product("10-0001-002", "シャツ")
    promo["variants"][0]["compare_at_price_jpy"] = 20000
    assert assess_daily_quote_product(promo, specials)[1] == LIMITED_TIME_PRICE
    assert assess_daily_quote_product(product("10-0001-003", "トートバッグ"), specials)[1] == NON_WHITELIST_CATEGORY
    service = product("10-0001-009", "名入れ代", tags=["baby"])
    service["product_type"] = "名入れ代商品"
    service["tags"] = ["名入れ代", "手数料商品"]
    assert assess_daily_quote_product(service, specials)[1] == NON_SELLABLE_SERVICE_OR_ADDON
    no_stock = product("10-0001-004", "シャツ")
    no_stock["variants"][0]["available_for_sale"] = False
    assert assess_daily_quote_product(no_stock, specials)[1] == ALL_VARIANTS_UNAVAILABLE
    assert assess_daily_quote_product(product("10-0001-005", "ベビー肌着", tags=["baby"]), specials)[0] == DAILY_QUOTE_ELIGIBLE


def test_manifest_contains_only_in_stock_variants_and_is_idempotent() -> None:
    row = product("10-0001-006", "シャツ", variants=[
        {"sku": "A", "color": "赤", "size": "80", "available_for_sale": True, "tax_included_price_jpy": 10000, "compare_at_price_jpy": 10000},
        {"sku": "B", "color": "紺", "size": "90", "available_for_sale": False, "tax_included_price_jpy": 10000, "compare_at_price_jpy": 10000},
    ])
    kwargs = dict(
        special_numbers=special_set(), fx=frozen_fx(), quote_date="2026-09-20",
        generated_at="2026-09-20T10:00:00+09:00",
    )
    first = build_daily_quote_manifest([row], **kwargs)
    second = build_daily_quote_manifest([row], **kwargs)
    assert first == second
    assert [value["sku"] for value in first["products"][0]["variants"]] == ["A"]
    assert first["manifest_sha256"] == second["manifest_sha256"]


def test_variant_price_groups_keep_exact_color_size_relationships() -> None:
    rows = [
        {"available_for_sale": True, "color": "赤", "size": "13", "customer_price_cny": 719},
        {"available_for_sale": True, "color": "赤", "size": "14", "customer_price_cny": 759},
        {"available_for_sale": True, "color": "紺", "size": "13", "customer_price_cny": 719},
        {"available_for_sale": False, "color": "紺", "size": "14", "customer_price_cny": 719},
    ]
    groups = group_available_variants(rows)
    assert groups == [
        {"customer_price_cny": 719, "colors": [{"color": "赤", "sizes": ["13"]}, {"color": "紺", "sizes": ["13"]}]},
        {"customer_price_cny": 759, "colors": [{"color": "赤", "sizes": ["14"]}]},
    ]


def test_ecb_cross_rate_and_weekend_latest_valid_rate() -> None:
    raw = b'''<?xml version="1.0"?><gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref"><Cube><Cube time="2026-09-18"><Cube currency="CNY" rate="8.2"/><Cube currency="JPY" rate="170"/></Cube></Cube></gesmes:Envelope>'''
    rate = parse_ecb_reference_rates(raw, datetime(2026, 9, 20, tzinfo=timezone.utc))
    assert rate.jpy_to_cny_rate == Decimal("8.2") / Decimal("170")
    assert_fx_fresh(rate, date(2026, 9, 20), 7)
    with pytest.raises(FxError):
        assert_fx_fresh(rate, date(2026, 9, 26), 7)


def test_fx_failure_is_closed_and_manual_override_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> object:
        raise OSError("offline")

    monkeypatch.setattr("mikihouse_luyao.daily_quote_fx.urlopen", fail)
    with pytest.raises(FxError, match="fetch failed"):
        fetch_ecb_reference_rate(quote_date=date(2026, 9, 20))
    override = manual_fx_rate("0.048", quote_date=date(2026, 9, 20))
    assert override.provider == "MANUAL_OVERRIDE"
    assert override.manual_override is True
    assert override.jpy_to_cny_rate == Decimal("0.048")


def test_text_quote_uses_manifest_only_and_contains_no_internal_pricing() -> None:
    manifest = build_daily_quote_manifest(
        [product("10-0001-007", "シャツ")], special_numbers=special_set(), fx=frozen_fx(),
        quote_date="2026-09-20", generated_at="2026-09-20T10:00:00+09:00",
    )
    text = render_text_quote(manifest)
    assert "10-0001-007" in text and "赤:13cm" in text and "元" in text
    for forbidden in ("16500", "0.68", "0.048", "JPY", "税入"):
        assert forbidden not in text
    assert text_stats(text, 1)["product_count"] == 1


def test_compact_text_is_lossless_for_price_color_and_in_stock_sizes() -> None:
    manifest = build_daily_quote_manifest(
        [product("10-0001-011", "ベビーシューズ", tags=["first-shoes"], variants=[
            {"sku": "A", "color": "赤", "size": "13cm", "available_for_sale": True, "tax_included_price_jpy": 10000, "compare_at_price_jpy": 10000},
            {"sku": "B", "color": "赤", "size": "13.5cm", "available_for_sale": True, "tax_included_price_jpy": 10000, "compare_at_price_jpy": 10000},
            {"sku": "C", "color": "紺", "size": "13cm", "available_for_sale": True, "tax_included_price_jpy": 10000, "compare_at_price_jpy": 10000},
            {"sku": "D", "color": "紺", "size": "13.5cm", "available_for_sale": True, "tax_included_price_jpy": 10000, "compare_at_price_jpy": 10000},
            {"sku": "E", "color": "赤", "size": "14cm", "available_for_sale": True, "tax_included_price_jpy": 12000, "compare_at_price_jpy": 12000},
        ])],
        special_numbers=special_set(), fx=frozen_fx(), quote_date="2026-09-20",
        generated_at="2026-09-20T10:00:00+09:00",
    )
    compact = render_compact_text_quote(manifest)
    assert "10-0001-011" in compact
    assert "327元 赤/紺:13/13.5" in compact
    assert "392元 赤:14" in compact
    assert "16500" not in compact and "0.68" not in compact and "JPY" not in compact


def test_pdf_and_text_share_manifest_variant_price_groups() -> None:
    manifest = build_daily_quote_manifest(
        [product("10-0001-010", "シャツ", variants=[
            {"sku": "A", "color": "赤", "size": "80", "available_for_sale": True, "tax_included_price_jpy": 10000, "compare_at_price_jpy": 10000},
            {"sku": "B", "color": "赤", "size": "90", "available_for_sale": True, "tax_included_price_jpy": 12000, "compare_at_price_jpy": 12000},
        ])],
        special_numbers=special_set(), fx=frozen_fx(), quote_date="2026-09-20",
        generated_at="2026-09-20T10:00:00+09:00",
    )
    row = manifest["products"][0]
    assert [group["customer_price_cny"] for group in row["variant_price_groups"]] == [327, 392]
    text = render_text_quote(manifest)
    assert "赤 80｜327元" in text
    assert "赤 90｜392元" in text


def test_pdf_search_layer_outlines_and_thumbnail_compression(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    Image.new("RGBA", (900, 600), (255, 0, 0, 128)).save(source)
    source_data = source.read_bytes()
    asset = {
        "content_sha256": __import__("hashlib").sha256(source_data).hexdigest(),
        "source_path": str(source),
    }
    thumb = create_thumbnail(asset, cache_dir=tmp_path / "cache", long_edge_px=360, jpeg_quality=70)
    products = []
    for index in range(60):
        kind = index % 3
        name = ("ベビーシューズ", "ベビー肌着", "シャツ")[kind]
        tags = (["first-shoes"], ["baby"], ["tops"])[kind]
        products.append(product(f"10-{index:04d}-001", name, tags=tags))
    manifest = build_daily_quote_manifest(
        products, special_numbers=special_set(), fx=frozen_fx(), quote_date="2026-09-20",
        generated_at="2026-09-20T10:00:00+09:00",
    )
    paths = {row["product_number"]: Path(thumb["thumbnail_path"]) for row in manifest["products"]}
    pdf_path = tmp_path / "daily.pdf"
    report = generate_daily_quote_pdf(manifest, paths, pdf_path)
    validation = validate_daily_quote_pdf(pdf_path, manifest, report, sample_size=50)
    extracted = "\n".join((page.extract_text() or "") for page in PdfReader(pdf_path).pages)
    assert validation["search_sample_count"] == 50
    assert validation["search_pass_rate"] == 1
    assert set(("鞋类", "婴幼儿", "服装")).issubset(validation["outline_titles"])
    assert "ベビーシューズ" in extracted and "赤" in extracted and "13cm" in extracted and "元" not in extracted
    assert "人民币" in extracted
    assert thumb["thumbnail_width"] <= 360 and thumb["thumbnail_height"] <= 360
    assert thumb["thumbnail_byte_count"] < len(source_data)


def test_oversize_pdf_uses_exactly_three_category_attachments(tmp_path: Path) -> None:
    full = tmp_path / "full.pdf"
    with full.open("wb") as handle:
        handle.truncate(51 * 1024 * 1024)
    categories = {}
    for key in ("footwear", "baby", "apparel"):
        path = tmp_path / f"{key}.pdf"
        path.write_bytes(b"pdf")
        categories[key] = path
    manifest = {"quote_date": "2026-09-20", "manifest_sha256": "a" * 64, "counts": {"included_in_stock_product_count": 1}}
    pdf_payload, text_payload = build_favorite_payloads(
        manifest, text_quote="quote", full_pdf=full, category_pdfs=categories, max_pdf_mb=50
    )
    assert pdf_payload["attachment_strategy"] == "THREE_CATEGORY_PDFS"
    assert len(pdf_payload["attachments"]) == 3
    assert text_payload["capacity_readiness"] == "REAL_WECHAT_TEXT_CAPACITY_NOT_YET_VERIFIED"


def test_incomplete_or_abnormally_small_crawl_fails_closed() -> None:
    config = {"crawl_minimum_product_count": 2500, "crawl_max_drop_ratio": "0.20"}
    with pytest.raises(DailyQuoteRunError):
        _validate_crawl([], {"storefront_product_count": 0}, config, None)


def test_incomplete_crawl_cannot_replace_last_successful_manifest(tmp_path: Path) -> None:
    last_successful = tmp_path / "last_successful_manifest.json"
    original = '{"manifest_sha256":"preserved"}\n'
    last_successful.write_text(original, encoding="utf-8")
    config = {"crawl_minimum_product_count": 2500, "crawl_max_drop_ratio": "0.20"}
    with pytest.raises(DailyQuoteRunError):
        _validate_crawl([], {"storefront_product_count": 0}, config, {"source_snapshot_product_count": 2987})
    assert last_successful.read_text(encoding="utf-8") == original


def test_daily_quote_modules_have_no_shijiu_endpoint_or_mutation_toggle() -> None:
    root = Path(__file__).resolve().parents[1]
    paths = list((root / "src" / "mikihouse_luyao").glob("daily_quote*.py"))
    text = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    assert "/shopapi/" not in text
    config = json.loads((root / "config" / "daily_quote.json").read_text(encoding="utf-8"))
    assert config["shijiu_requests_enabled"] is False
    assert config["wechat_write_enabled"] is False
