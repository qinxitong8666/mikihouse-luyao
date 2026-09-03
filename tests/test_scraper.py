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


def test_rejects_product_number_mismatch() -> None:
    with pytest.raises(ScrapeError, match="mismatch"):
        parse_product_html(FIXTURE.read_text(encoding="utf-8"), "99-9999-999")


def test_parses_storefront_api_response() -> None:
    payload = {
        "data": {"product": {
            "title": "ミキハウスベア ベビーフォーマルセット",
            "handle": "10-1105-495",
            "featuredImage": {"url": "https://cdn.shopify.com/main.jpg"},
            "variants": {"nodes": [
                {"title": "紺 / 80cm", "sku": "one", "availableForSale": True,
                 "price": {"amount": "44000.0", "currencyCode": "JPY"}},
                {"title": "紺 / 90cm", "sku": "two", "availableForSale": False,
                 "price": {"amount": "44000.0", "currencyCode": "JPY"}},
            ]},
        }}
    }
    product = parse_storefront_response(payload, "10-1105-495")
    assert product.pdf_price == 1398
    assert [(item.size, item.in_stock) for item in product.variants] == [("80cm", True), ("90cm", False)]
