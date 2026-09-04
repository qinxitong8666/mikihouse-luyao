from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from mikihouse_luyao.shijiu_import import map_product_to_shijiu
from mikihouse_luyao.shijiu_live_import import LiveImportError
from mikihouse_luyao.shijiu_richtext_e2e import (
    FROZEN_PRODUCT_NUMBER,
    RICHTEXT_E2E_MODE,
    build_richtext_e2e_conclusion,
    load_richtext_e2e_candidate,
    verify_live_source_exact,
)
from mikihouse_luyao.shijiu_staged_media_import import stage_plan


ROOT = Path(__file__).resolve().parents[1]
CATEGORY = {
    "id": 294884,
    "name": "MikiHouse",
    "parent_id": 288338,
    "parent_name": "母婴用品",
    "assignment_policy": "all_publishable_mikihouse_products",
}


def exclusions() -> set[str]:
    return {f"99-{index:04d}-999" for index in range(351)}


def source_product() -> dict:
    images = [
        {
            "order": index,
            "role": "main" if index == 1 else "detail" if index == 18 else "product_gallery",
            "image": {"url": f"https://cdn.shopify.com/{index}.jpg", "width": 900, "height": 900},
        }
        for index in range(1, 19)
    ]
    variants = []
    for color_code, color in (("01", "白"), ("06", "グレー")):
        for size in ("13cm", "14cm", "15cm"):
            sku = f"{FROZEN_PRODUCT_NUMBER.replace('-', '')}{color_code}{size[:2]}"
            variants.append({
                "sku": sku,
                "selected_options": [
                    {"name": "カラー", "value": color},
                    {"name": "サイズ", "value": size},
                ],
                "color": color,
                "size": size,
                "active": True,
                "available_for_sale": True,
                "tax_included_price_jpy": 55000,
                "mini_program_price_jpy": 35750,
                "resolved_image": images[0]["image"],
            })
    return {
        "product_number": FROZEN_PRODUCT_NUMBER,
        "name": "牛革セカンドベビーシューズ",
        "active": True,
        "brand": "MIKI HOUSE",
        "category": {"name": "シューズ"},
        "product_type": "シューズ",
        "tags": [],
        "description": '<p>説明</p><img src="https://cdn.shopify.com/detail.jpg">',
        "main_image": images[0]["image"],
        "ordered_images": images,
        "variants": variants,
    }


def test_loader_enforces_exact_frozen_product_and_five_stages() -> None:
    source = source_product()
    item = map_product_to_shijiu(source, CATEGORY, excluded_product_numbers=exclusions())
    selection = {
        "mode": RICHTEXT_E2E_MODE,
        "fixed_target_category_id": 294884,
        "historical_prohibited_product_numbers": [],
        "product": {
            "product_number": FROZEN_PRODUCT_NUMBER,
            "variant_count": 6,
            "broadcast_count": 18,
            "detail_pic_count": 16,
            "source_payload_sha256": item["payload_sha256"],
        },
        "stages": stage_plan(item),
    }
    assert load_richtext_e2e_candidate(
        {"products": [source]}, exclusions(), CATEGORY, selection
    )["product_number"] == FROZEN_PRODUCT_NUMBER
    changed = copy.deepcopy(selection)
    changed["product"]["product_number"] = "10-0000-000"
    with pytest.raises(LiveImportError, match="selection boundary"):
        load_richtext_e2e_candidate({"products": [source]}, exclusions(), CATEGORY, changed)


def test_live_source_verifier_checks_65_percent_price_and_options() -> None:
    source = source_product()
    item = map_product_to_shijiu(source, CATEGORY, excluded_product_numbers=exclusions())

    class Option:
        def __init__(self, name: str, value: str) -> None:
            self.name, self.value = name, value

    class Variant:
        def __init__(self, row: dict) -> None:
            self.sku = row["sku"]
            self.tax_included_price_jpy = row["tax_included_price_jpy"]
            self.in_stock = row["available_for_sale"]
            self.color = row["color"]
            self.size = row["size"]
            self.image_url = row["resolved_image"]["url"]
            self.selected_options = tuple(
                Option(option["name"], option["value"]) for option in row["selected_options"]
            )

    live = type("Live", (), {
        "product_number": FROZEN_PRODUCT_NUMBER,
        "name": source["name"],
        "variants": tuple(Variant(row) for row in source["variants"]),
    })()
    result = verify_live_source_exact(item, live)
    assert result["variant_count"] == 6
    assert {row["mini_program_price_jpy"] for row in result["variants"]} == {35750}
    assert result["all_skus_prices_65pct_prices_stocks_options_and_images_match_master"] is True


def test_completed_conclusion_fixes_lightweight_production_architecture() -> None:
    checkpoint = {
        "status": "COMPLETED",
        "product_number": FROZEN_PRODUCT_NUMBER,
        "shijiu_product_id": "123",
        "resource_preflight": {"status": "PASSED"},
        "request_ledger": [],
        "first_failed_state": None,
        "stages": [{
            "key": "DETAIL_PICS_9_16",
            "operation": "UPDATE_DETAIL_PICS",
            "state": "VERIFIED",
            "metrics": {
                "broadcast_url_count": 18,
                "good_detail_pics_url_count": 16,
                "good_details_characters": 200,
                "good_details_image_count": 0,
                "good_details_url_count": 0,
            },
        }],
    }
    conclusion = build_richtext_e2e_conclusion(checkpoint)
    assert conclusion["technical_five_stage_readback_completed"] is True
    assert conclusion["production_import_architecture_verified"] is False
    assert conclusion["production_write_mutex_evidence_verified"] is False
    assert conclusion["fail_closed_no_further_write"] is True
    assert conclusion["image_type_good_details_generated_or_attempted"] is False
    assert "GOOD_DETAIL_PICS" in conclusion["production_architecture"]


def test_checked_in_richtext_contract_remains_no_image_no_url() -> None:
    contract = json.loads((ROOT / "config/shijiu_richtext_contract.json").read_text())
    assert contract["good_details"]["maximum_characters"] == 1024
    assert contract["good_details"]["embedded_image_tags_allowed"] is False
    assert contract["good_details"]["embedded_urls_allowed"] is False
