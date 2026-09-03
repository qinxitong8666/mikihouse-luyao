from pathlib import Path

import pytest

import mikihouse_luyao.scraper as scraper
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


def test_storefront_preserves_footwear_options_images_and_availability() -> None:
    payload = {
        "data": {"product": {
            "title": "ファーストベビーシューズ",
            "handle": "10-9301-495",
            "productType": "通常商品",
            "tags": ["shoes", "ファーストシューズ"],
            "featuredImage": {"url": "https://cdn.shopify.com/main.jpg"},
            "variants": {"pageInfo": {"hasNextPage": False}, "nodes": [
                {
                    "title": "白 / 11.5cm", "sku": "white-115", "availableForSale": True,
                    "selectedOptions": [{"name": "カラー", "value": "白"}, {"name": "サイズ", "value": "11.5cm"}],
                    "image": {"url": "https://cdn.shopify.com/white.jpg", "width": 3000, "height": 3000},
                    "price": {"amount": "13200.0", "currencyCode": "JPY"},
                },
                {
                    "title": "赤 / 12cm", "sku": "red-120", "availableForSale": False,
                    "selectedOptions": [{"name": "カラー", "value": "赤"}, {"name": "サイズ", "value": "12cm"}],
                    "image": {"url": "https://cdn.shopify.com/red.jpg", "width": 2400, "height": 2400},
                    "price": {"amount": "14300.0", "currencyCode": "JPY"},
                },
            ]},
        }}
    }
    product = parse_storefront_response(payload, "10-9301-495")
    assert product.is_footwear
    assert product.tags == ("shoes", "ファーストシューズ")
    assert [(variant.color, variant.size, variant.sku, variant.in_stock) for variant in product.variants] == [
        ("白", "11.5cm", "white-115", True),
        ("赤", "12cm", "red-120", False),
    ]
    assert [variant.image_url for variant in product.variants] == [
        "https://cdn.shopify.com/white.jpg", "https://cdn.shopify.com/red.jpg"
    ]
    assert product.to_dict()["variants"][0]["available_for_sale"] is True


def test_storefront_fetch_paginates_variants(monkeypatch) -> None:
    def payload(node, has_next, cursor):
        return {"data": {"product": {
            "title": "テスト商品", "handle": "10-1105-495", "productType": "通常商品", "tags": [],
            "featuredImage": {"url": "https://cdn.shopify.com/main.jpg"},
            "variants": {
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                "nodes": [{
                    "title": f"紺 / {node}cm", "sku": f"sku-{node}", "availableForSale": True,
                    "selectedOptions": [{"name": "カラー", "value": "紺"}, {"name": "サイズ", "value": f"{node}cm"}],
                    "image": {"url": "https://cdn.shopify.com/navy.jpg", "width": 1200, "height": 1200},
                    "price": {"amount": "11000.0", "currencyCode": "JPY"},
                }],
            },
        }}}

    pages = {None: payload("80", True, "cursor-1"), "cursor-1": payload("90", False, "cursor-2")}
    monkeypatch.setattr(scraper, "_fetch_storefront_page", lambda number, after, timeout, retries: pages[after])
    product = scraper._fetch_storefront("10-1105-495", 1, 0)
    assert [(variant.size, variant.sku) for variant in product.variants] == [("80cm", "sku-80"), ("90cm", "sku-90")]


def test_footwear_classification_rejects_broad_navigation_tags() -> None:
    assert scraper.is_footwear_product("キッズシューズ", ["shoes"])
    assert scraper.is_footwear_product("ラムスキン・ホーススキン ローファー", ["formalshoes"])
    assert not scraper.is_footwear_product("海島綿ソックス", ["AHシューズ・ソックス"])
    assert not scraper.is_footwear_product("シルクレースセレモニードレスセット", ["ceremony-shoes", "shoes"])
