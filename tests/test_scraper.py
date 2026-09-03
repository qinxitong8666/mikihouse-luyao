from pathlib import Path

import pytest

from mikihouse_luyao.scraper import ScrapeError, parse_product_html, parse_storefront_response


FIXTURE = Path(__file__).parent / "fixtures" / "10-1105-495.html"


def test_parses_and_validates_requested_product() -> None:
    product = parse_product_html(FIXTURE.read_text(encoding="utf-8"), "10-1105-495")
    assert product.name == "ミキハウスベア ベビーフォーマルセット"
    assert product.tax_included_price_jpy == 44_000
    assert product.pdf_price == 1_398
    assert product.main_image_url.startswith("https://www.mikihouse.co.jp/cdn/shop/products/")
    assert [(v.color, v.size, v.stock_text) for v in product.variants] == [
        ("紺", "80cm", "在庫あり"),
        ("紺", "90cm", "残り4点"),
    ]
    assert {(v.tax_included_price_jpy, v.pdf_price) for v in product.variants} == {(44_000, 1_398)}


def test_rejects_product_number_mismatch() -> None:
    with pytest.raises(ScrapeError, match="mismatch"):
        parse_product_html(FIXTURE.read_text(encoding="utf-8"), "99-9999-999")


def test_html_parser_preserves_different_variant_prices() -> None:
    html = FIXTURE.read_text(encoding="utf-8").replace('"price":"44000"', '"price":"46200"', 1)
    product = parse_product_html(html, "10-1105-495")
    assert product.tax_included_price_jpy is None
    assert [(variant.tax_included_price_jpy, variant.pdf_price) for variant in product.variants] == [
        (46_200, 1_468),
        (44_000, 1_398),
    ]


def test_parses_different_variant_prices_without_rejecting_product() -> None:
    payload = {
        "data": {"product": {
            "title": "ミキハウスベア ベビーフォーマルセット",
            "handle": "10-1105-495",
            "featuredImage": {"url": "https://cdn.shopify.com/main.jpg"},
            "variants": {"nodes": [
                {"title": "紺 / 80cm", "sku": "one", "availableForSale": True,
                 "price": {"amount": "44000.0", "currencyCode": "JPY"}},
                {"title": "紺 / 90cm", "sku": "two", "availableForSale": False,
                 "price": {"amount": "46200.0", "currencyCode": "JPY"}},
            ]},
        }}
    }
    product = parse_storefront_response(payload, "10-1105-495")
    assert product.tax_included_price_jpy is None
    assert product.pdf_price is None
    assert [(item.size, item.in_stock) for item in product.variants] == [("80cm", True), ("90cm", False)]
    assert [(item.tax_included_price_jpy, item.pdf_price) for item in product.variants] == [
        (44_000, 1_398),
        (46_200, 1_468),
    ]
    data = product.to_dict()
    assert data["tax_included_price_jpy_min"] == 44_000
    assert data["tax_included_price_jpy_max"] == 46_200
