from __future__ import annotations

import copy

import pytest

import mikihouse_luyao.catalog as catalog
from mikihouse_luyao.catalog import (
    build_validation_report,
    calculate_mini_program_price_jpy,
    change_rows,
    merge_catalog,
    normalize_product,
    product_rows,
    variant_rows,
)


def raw_product(handle: str = "20-0001-001", price: str = "11001.0") -> dict:
    return {
        "id": f"gid://shopify/Product/{handle}",
        "handle": handle,
        "title": "ベビーシューズ",
        "vendor": "ミキハウス",
        "productType": "シューズ",
        "tags": ["shoes", "ベビー"],
        "category": {"id": "gid://shopify/TaxonomyCategory/aa", "name": "Baby Shoes"},
        "onlineStoreUrl": f"https://www.mikihouse.co.jp/products/{handle}",
        "description": "公式の商品説明",
        "descriptionHtml": '<p>公式の商品説明</p><img src="https://cdn/detail.jpg">',
        "featuredImage": {"url": "https://cdn/main.jpg", "width": 3000, "height": 3000, "altText": "main"},
        "images": {
            "pageInfo": {"hasNextPage": False, "endCursor": "i1"},
            "nodes": [
                {"url": "https://cdn/main.jpg", "width": 3000, "height": 3000, "altText": "main"},
                {"url": "https://cdn/angle.jpg", "width": 2500, "height": 2500, "altText": "angle"},
                {"url": "https://cdn/red.jpg", "width": 2400, "height": 2400, "altText": "red"},
            ],
        },
        "media": {
            "pageInfo": {"hasNextPage": False, "endCursor": "m1"},
            "nodes": [{
                "id": "gid://shopify/MediaImage/1",
                "mediaContentType": "IMAGE",
                "alt": "detail",
                "previewImage": {"url": "https://cdn/media-detail.jpg", "width": 2000, "height": 2000, "altText": "detail"},
                "image": {"url": "https://cdn/media-detail.jpg", "width": 2000, "height": 2000, "altText": "detail"},
            }],
        },
        "variants": {
            "pageInfo": {"hasNextPage": False, "endCursor": "v1"},
            "nodes": [
                {
                    "id": "gid://shopify/ProductVariant/1",
                    "title": "赤 / 12.5cm",
                    "sku": f"{handle}-red-125",
                    "availableForSale": True,
                    "selectedOptions": [
                        {"name": "カラー", "value": "赤"},
                        {"name": "サイズ", "value": "12.5cm"},
                    ],
                    "image": {"url": "https://cdn/red.jpg", "width": 2400, "height": 2400, "altText": "red"},
                    "price": {"amount": price, "currencyCode": "JPY"},
                }
            ],
        },
    }


def test_mini_program_price_is_ceil_65_percent_jpy_only() -> None:
    assert calculate_mini_program_price_jpy(11_000) == 7_150
    assert calculate_mini_program_price_jpy(11_001) == 7_151
    with pytest.raises(ValueError):
        calculate_mini_program_price_jpy(-1)


def test_normalize_product_preserves_storefront_variant_fields_and_image_mapping() -> None:
    product = normalize_product(raw_product(), "2026-09-03T00:00:00+00:00")
    variant = product["variants"][0]
    assert product["product_number"] == product["handle"] == "20-0001-001"
    assert product["brand"] == "ミキハウス"
    assert product["category"]["name"] == "Baby Shoes"
    assert variant["stable_id"] == "20-0001-001::20-0001-001-red-125"
    assert variant["selected_options"][0] == {"name": "カラー", "value": "赤"}
    assert variant["color"] == "赤"
    assert variant["size"] == "12.5cm"
    assert variant["available_for_sale"] is True
    assert variant["tax_included_price_jpy"] == 11_001
    assert variant["mini_program_price_jpy"] == 7_151
    assert variant["variant_image"]["url"] == "https://cdn/red.jpg"
    assert product["color_images"][0]["color"] == "赤"
    assert product["description"] == "公式の商品説明"
    assert len(product["product_images"]) == 3
    assert len(product["media"]) == 1
    assert [item["role"] for item in product["ordered_images"]] == [
        "main", "product_gallery", "variant_color", "detail", "detail"
    ]
    ordered_urls = [item["image"]["url"] for item in product["ordered_images"]]
    assert len(ordered_urls) == len(set(ordered_urls))


def test_full_catalog_fetch_paginates_products_variants_and_excludes_special(monkeypatch) -> None:
    first = raw_product("10-1105-495")
    second = raw_product("20-0001-001")
    second["variants"]["pageInfo"] = {"hasNextPage": True, "endCursor": "variant-1"}
    second["variants"]["nodes"] = []
    third = raw_product("30-0001-001")
    pages = {
        (catalog.CATALOG_QUERY, None): {
            "data": {"products": {"pageInfo": {"hasNextPage": True, "endCursor": "product-1"}, "nodes": [first, second]}}
        },
        (catalog.VARIANT_PAGE_QUERY, "variant-1"): {
            "data": {"product": {"handle": "20-0001-001", "variants": raw_product("20-0001-001")["variants"]}}
        },
        (catalog.CATALOG_QUERY, "product-1"): {
            "data": {"products": {"pageInfo": {"hasNextPage": False, "endCursor": "done"}, "nodes": [third]}}
        },
    }

    def fake_request(query, variables, timeout, retries):
        return copy.deepcopy(pages[(query, variables.get("after"))])

    monkeypatch.setattr(catalog, "_graphql_request", fake_request)
    products, stats = catalog.fetch_all_storefront_products({"10-1105-495"}, page_size=2, delay=0)
    assert [item["product_number"] for item in products] == ["20-0001-001", "30-0001-001"]
    assert stats["storefront_product_count"] == 3
    assert stats["excluded_special_product_count"] == 1
    assert stats["excluded_special_not_present_count"] == 0
    assert stats["product_page_count"] == 2
    assert stats["extra_variant_page_count"] == 1


def test_full_catalog_fetch_paginates_product_images_and_media(monkeypatch) -> None:
    item = raw_product("20-0001-001")
    item["images"]["pageInfo"] = {"hasNextPage": True, "endCursor": "image-1"}
    item["media"]["pageInfo"] = {"hasNextPage": True, "endCursor": "media-1"}
    calls = []

    def fake_request(query, variables, timeout, retries):
        calls.append((query, variables.get("after")))
        if query == catalog.CATALOG_QUERY:
            return {"data": {"products": {
                "pageInfo": {"hasNextPage": False, "endCursor": "done"},
                "nodes": [copy.deepcopy(item)],
            }}}
        if query == catalog.IMAGE_PAGE_QUERY:
            return {"data": {"product": {"handle": item["handle"], "images": {
                "pageInfo": {"hasNextPage": False, "endCursor": "image-done"},
                "nodes": [{"url": "https://cdn/last-angle.jpg", "width": 1000, "height": 1000}],
            }}}}
        return {"data": {"product": {"handle": item["handle"], "media": {
            "pageInfo": {"hasNextPage": False, "endCursor": "media-done"},
            "nodes": [{
                "id": "gid://shopify/MediaImage/2", "mediaContentType": "IMAGE",
                "image": {"url": "https://cdn/last-detail.jpg", "width": 1000, "height": 1000},
            }],
        }}}}

    monkeypatch.setattr(catalog, "_graphql_request", fake_request)
    products, stats = catalog.fetch_all_storefront_products(set(), page_size=1, delay=0)
    assert len(products[0]["product_images"]) == 4
    assert len(products[0]["media"]) == 2
    assert stats["extra_image_page_count"] == 1
    assert stats["extra_media_page_count"] == 1
    assert (catalog.IMAGE_PAGE_QUERY, "image-1") in calls
    assert (catalog.MEDIA_PAGE_QUERY, "media-1") in calls


def test_incremental_merge_detects_price_inventory_image_inactive_and_restore() -> None:
    first_time = "2026-09-03T00:00:00+00:00"
    second_time = "2026-09-04T00:00:00+00:00"
    third_time = "2026-09-05T00:00:00+00:00"
    initial_product = normalize_product(raw_product(), first_time)
    master, _ = merge_catalog(None, [initial_product], set(), first_time)

    changed_raw = raw_product(price="13201.0")
    changed_raw["variants"]["nodes"][0]["availableForSale"] = False
    changed_raw["variants"]["nodes"][0]["image"]["url"] = "https://cdn/red-new.jpg"
    changed_product = normalize_product(changed_raw, second_time)
    changed_master, report = merge_catalog(master, [changed_product], set(), second_time)
    change_types = {item["change_type"] for item in report["changes"]}
    assert {"price_changed", "inventory_changed", "variant_image_changed", "product_images_changed"} <= change_types

    inactive_master, inactive_report = merge_catalog(changed_master, [], set(), third_time)
    assert inactive_master["products"][0]["active"] is False
    assert inactive_master["products"][0]["variants"][0]["active"] is False
    assert {item["change_type"] for item in inactive_report["changes"]} == {
        "product_inactivated",
        "variant_inactivated",
    }

    restored, restored_report = merge_catalog(inactive_master, [changed_product], set(), "2026-09-06T00:00:00+00:00")
    assert restored["products"][0]["active"] is True
    assert restored["products"][0]["variants"][0]["active"] is True
    assert {"product_reactivated", "variant_reactivated"} <= {
        item["change_type"] for item in restored_report["changes"]
    }


def test_validation_requires_four_categories_and_no_special_leak() -> None:
    synced_at = "2026-09-03T00:00:00+00:00"
    products = []
    definitions = [
        ("20-0001-001", "キッズシューズ", "シューズ", ["shoes"]),
        ("20-0001-002", "長袖Tシャツ", "ウェア", ["トップス"]),
        ("20-0001-003", "ベビースタイ", "ベビー用品", ["ベビー"]),
        ("20-0001-004", "ミニタオル", "雑貨", ["タオル"]),
    ]
    for handle, title, product_type, tags in definitions:
        raw = raw_product(handle)
        raw["title"] = title
        raw["productType"] = product_type
        raw["tags"] = tags
        products.append(normalize_product(raw, synced_at))
    master, _ = merge_catalog(None, products, {"10-1105-495"}, synced_at)
    report = build_validation_report(master, {"10-1105-495"}, synced_at)
    assert report["passed"] is True
    assert all(report["examples"][kind] for kind in ("footwear", "apparel", "baby", "goods"))

    master["products"][0]["product_number"] = "10-1105-495"
    with pytest.raises(catalog.ScrapeError, match="special products leaked"):
        build_validation_report(master, {"10-1105-495"}, synced_at)


def test_standard_csv_rows_keep_jpy_prices_and_stable_keys() -> None:
    synced_at = "2026-09-03T00:00:00+00:00"
    product = normalize_product(raw_product(), synced_at)
    master, changes = merge_catalog(None, [product], set(), synced_at)
    product_fields, product_values = product_rows(master)
    variant_fields, variant_values = variant_rows(master)
    change_fields, change_values = change_rows(changes)

    assert product_values[0][product_fields.index("product_number")] == "20-0001-001"
    assert variant_values[0][variant_fields.index("stable_id")].startswith("20-0001-001::")
    assert variant_values[0][variant_fields.index("tax_included_price_jpy")] == 11_001
    assert variant_values[0][variant_fields.index("mini_program_price_jpy")] == 7_151
    assert change_values[0][change_fields.index("change_type")] == "new_product"
    assert not any("rmb" in field or "cny" in field or "exchange" in field for field in variant_fields)
