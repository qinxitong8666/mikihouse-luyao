from __future__ import annotations

import copy
import pytest

from mikihouse_luyao.shijiu_import import ImportPlanError, build_parser, map_product_to_shijiu
from mikihouse_luyao.stable_catalog import (
    EXCLUDED,
    LIMITED_TIME_PRICE,
    NON_SELLABLE_SERVICE_OR_ADDON,
    PDF_SPECIAL,
    REVIEW_REQUIRED,
    STABLE,
    WEB_EXCLUSIVE,
    assess_product_stability,
    partition_stable_catalog,
)


CATEGORY = {"id": 294884, "name": "MikiHouse", "parent_id": 288338}


def product(number: str = "20-0001-001") -> dict:
    image = {"url": "https://cdn.shopify.com/main.jpg", "width": 1000, "height": 1000}
    return {
        "product_number": number,
        "handle": number,
        "name": "定番ベビーシューズ",
        "brand": "ミキハウス",
        "product_type": "シューズ",
        "category": {"id": "source", "name": "Shoes"},
        "tags": ["shoes"],
        "description": "公式の商品説明です。",
        "description_html": "<p>公式の商品説明です。</p>",
        "main_image": image,
        "ordered_images": [{"order": 1, "role": "main", "image": image}],
        "color_images": [{"color": "赤", "images": [{"image": image}]}],
        "product_url": f"https://www.mikihouse.co.jp/products/{number}",
        "active": True,
        "variants": [{
            "stable_id": f"{number}::sku-1",
            "sku": "sku-1",
            "title": "赤 / 12cm",
            "active": True,
            "available_for_sale": True,
            "selected_options": [
                {"name": "カラー", "value": "赤"},
                {"name": "サイズ", "value": "12cm"},
            ],
            "color": "赤",
            "size": "12cm",
            "tax_included_price_jpy": 11_000,
            "compare_at_price_jpy": None,
            "mini_program_price_jpy": 7_150,
            "resolved_image": image,
        }],
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "【WEB限定】ベビーシューズ"),
        ("name", "Baby Shoes [WEB LIMITED]"),
        ("description", "こちらは WebLimited 商品です"),
        ("description", "オンラインショップ限定商品です"),
        ("description", "Online Exclusive item"),
        ("tags", ["shoes", "weblimited"]),
    ],
)
def test_explicit_web_exclusive_synonyms_are_permanently_excluded(field, value) -> None:
    item = product()
    item[field] = value
    decision = assess_product_stability(item, set())
    assert decision["status"] == EXCLUDED
    assert decision["excluded_reason"] == WEB_EXCLUSIVE


def test_compare_at_discount_and_explicit_limited_price_are_excluded() -> None:
    item = product()
    item["variants"][0]["compare_at_price_jpy"] = 13_200
    assert assess_product_stability(item, set())["excluded_reason"] == LIMITED_TIME_PRICE
    item = product()
    item["name"] = "【期間限定価格】ベビーシューズ"
    assert assess_product_stability(item, set())["excluded_reason"] == LIMITED_TIME_PRICE


def test_ambiguous_signals_require_review_but_reservation_is_only_reported() -> None:
    item = product()
    item["tags"] = ["webitem"]
    assert assess_product_stability(item, set())["status"] == REVIEW_REQUIRED
    item = product()
    item["description"] = "期間限定カラー"
    assert assess_product_stability(item, set())["status"] == REVIEW_REQUIRED
    item = product()
    item["description"] = "予約商品"
    assert assess_product_stability(item, set())["status"] == STABLE


def test_non_https_image_resource_requires_review_before_shijiu_planning() -> None:
    item = product()
    item["ordered_images"][0]["image"]["url"] = "http://image.example.invalid/detail.gif"
    decision = assess_product_stability(item, set())
    assert decision["status"] == REVIEW_REQUIRED
    assert decision["evidence"][0]["signal"] == "non_https_or_unqualified_official_image_resource"


def test_pdf_special_has_highest_priority() -> None:
    item = product("10-1105-495")
    item["name"] = "【WEB限定】【期間限定価格】商品"
    decision = assess_product_stability(item, {"10-1105-495"})
    assert decision["excluded_reason"] == PDF_SPECIAL


@pytest.mark.parametrize(
    "product_type,tags",
    [
        ("名入れ代商品", ["名入れ代", "手数料商品"]),
        ("ノベルティ商品", ["ノベルティ商品"]),
        ("メッセージカード商品", ["メッセージカード", "手数料商品"]),
        ("ギフトラッピング商品", ["ギフトラッピング商品", "手数料商品"]),
    ],
)
def test_explicit_official_service_or_addon_types_are_permanently_excluded(
    product_type: str, tags: list[str]
) -> None:
    item = product()
    item["product_type"] = product_type
    item["tags"] = tags
    decision = assess_product_stability(item, set())
    assert decision["status"] == EXCLUDED
    assert decision["excluded_reason"] == NON_SELLABLE_SERVICE_OR_ADDON
    assert decision["evidence"][0]["classification_does_not_depend_on_zero_price"] is True


def test_zero_price_alone_requires_review_and_never_implies_service() -> None:
    item = product()
    item["variants"][0]["tax_included_price_jpy"] = 0
    item["variants"][0]["mini_program_price_jpy"] = 0
    decision = assess_product_stability(item, set())
    assert decision["status"] == REVIEW_REQUIRED
    assert decision["excluded_reason"] == REVIEW_REQUIRED
    assert decision["evidence"][0]["not_automatically_classified_as_service_from_price"] is True


@pytest.mark.parametrize("field", ["main", "ordered", "variant"])
def test_sellable_missing_required_media_requires_review(field: str) -> None:
    item = product()
    if field == "main":
        item["main_image"] = None
    elif field == "ordered":
        item["ordered_images"] = []
    else:
        item["variants"][0]["resolved_image"] = None
    decision = assess_product_stability(item, set())
    assert decision["status"] == REVIEW_REQUIRED
    assert any(row["signal"] == "missing_required_sellable_media" for row in decision["evidence"])


def test_partition_adds_content_and_resource_hashes_only_to_stable_products() -> None:
    regular = product()
    web = product("20-0001-002")
    web["name"] = "【WEB限定】商品"
    result = partition_stable_catalog([regular, web], set(), "2026-09-05T00:00:00+00:00")
    assert len(result["stable_products"]) == 1
    stable = result["stable_products"][0]
    assert len(stable["source_content_sha256"]) == 64
    assert len(stable["image_resources"][0]["source_url_sha256"]) == 64
    assert "http" not in stable["shijiu_good_details"]
    assert result["excluded_products"][0]["excluded_reason"] == WEB_EXCLUSIVE


def test_shijiu_mapper_fails_closed_for_every_unstable_class() -> None:
    for mutation, reason in (
        (("name", "【WEB限定】商品"), WEB_EXCLUSIVE),
        (("name", "【期間限定価格】商品"), LIMITED_TIME_PRICE),
        (("tags", ["webitem"]), REVIEW_REQUIRED),
    ):
        item = product()
        item[mutation[0]] = mutation[1]
        with pytest.raises(ImportPlanError, match=reason):
            map_product_to_shijiu(item, CATEGORY, excluded_product_numbers=set())


def test_stable_hash_is_independent_of_capture_timestamp() -> None:
    first = partition_stable_catalog([product()], set(), "2026-09-05T00:00:00+00:00")
    second = partition_stable_catalog([copy.deepcopy(product())], set(), "2026-09-06T00:00:00+00:00")
    assert (
        first["stable_products"][0]["source_content_sha256"]
        == second["stable_products"][0]["source_content_sha256"]
    )


def test_shijiu_planner_defaults_to_stable_catalog_only() -> None:
    args = build_parser().parse_args([])
    assert str(args.master) == "output/storefront-stable/stable_catalog.json"
    assert str(args.changes) == "output/storefront-stable/stable_incremental_changes.json"
