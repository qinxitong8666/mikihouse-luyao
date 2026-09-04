from __future__ import annotations

import json
from pathlib import Path

import pytest

from mikihouse_luyao.shijiu_import import map_product_to_shijiu
from mikihouse_luyao.shijiu_live_import import LiveImportError
from mikihouse_luyao.shijiu_production_architecture_verification import (
    MINIMUM_DETAIL_PICS,
    build_final_e2e_conclusion,
    load_final_e2e_candidate,
)
from mikihouse_luyao.shijiu_staged_media_import import build_stage_payload, stage_plan


CATEGORY = {
    "id": 294884,
    "name": "MikiHouse",
    "parent_id": 288338,
    "parent_name": "母婴用品",
    "assignment_policy": "all_publishable_mikihouse_products",
}


def exclusions() -> set[str]:
    return {f"99-{index:04d}-999" for index in range(351)}


def product(number: str = "20-9000-016") -> dict:
    images = []
    for index in range(1, 21):
        role = "main" if index == 1 else "detail" if index >= 17 else "product_gallery"
        images.append({
            "order": index,
            "role": role,
            "image": {"url": f"https://cdn.shopify.com/{number}-{index}.jpg", "width": 900, "height": 900},
        })
    return {
        "product_number": number,
        "name": "最终闭环测试商品",
        "active": True,
        "brand": "MIKI HOUSE",
        "category": {"name": "雑貨"},
        "product_type": "雑貨",
        "tags": [],
        "description": '<p>説明</p><img src="https://cdn.shopify.com/detail.jpg">',
        "main_image": images[0]["image"],
        "ordered_images": images,
        "variants": [
            {
                "sku": f"{number.replace('-', '')}{index}",
                "selected_options": [{"name": "カラー", "value": color}],
                "color": color,
                "size": "",
                "active": True,
                "available_for_sale": True,
                "tax_included_price_jpy": 2200,
                "mini_program_price_jpy": 1430,
                "resolved_image": images[index]["image"],
            }
            for index, color in enumerate(("赤", "紺"), start=1)
        ],
    }


def test_stage_contract_keeps_detail_pics_out_of_minimal_html_until_final_stage() -> None:
    item = map_product_to_shijiu(product(), CATEGORY, excluded_product_numbers=exclusions())
    stages = stage_plan(item)
    detail = [row for row in stages if row["operation"] == "UPDATE_DETAIL_PICS"]
    assert [row["detail_pic_count"] for row in detail][:2] == [8, 16]
    assert stages[-1]["operation"] == "UPDATE_GOOD_DETAILS"
    uploads = {
        row["upload_reference"]: {"status": "UPLOADED", "target_url": f"https://cdn0.19mini.com/{row['order']}.jpg"}
        for row in item["image_upload_plan"]
    }
    intermediate = build_stage_payload(item, detail[1], uploads, product_id="1")
    assert intermediate["good_detail_pics"].count(",") + 1 == MINIMUM_DETAIL_PICS
    assert "cdn0.19mini.com" not in intermediate["good_details"]
    final = build_stage_payload(item, stages[-1], uploads, product_id="1")
    assert "cdn0.19mini.com" in final["good_details"]
    assert "shopify" not in final["good_details"]


def test_loader_rejects_frozen_selection_below_sixteen_detail_pics() -> None:
    source = product()
    item = map_product_to_shijiu(source, CATEGORY, excluded_product_numbers=exclusions())
    selection = {
        "mode": "MIKIHOUSE_PRODUCTION_ARCHITECTURE_FINAL_E2E_VALIDATION",
        "fixed_target_category_id": 294884,
        "historical_prohibited_product_numbers": [],
        "product": {
            "product_number": source["product_number"],
            "detail_pic_count": 15,
            "source_payload_sha256": item["payload_sha256"],
        },
        "stages": stage_plan(item),
    }
    with pytest.raises(LiveImportError, match="selection boundary"):
        load_final_e2e_candidate({"products": [source]}, exclusions(), CATEGORY, selection)


def test_production_conclusion_requires_completed_final_html_and_sixteen_details() -> None:
    checkpoint = {
        "status": "COMPLETED",
        "product_number": "20-9000-016",
        "shijiu_product_id": "123",
        "resource_preflight": {"status": "PASSED"},
        "request_ledger": [],
        "first_failed_state": None,
        "stages": [
            {"key": "DETAIL_PICS_9_16", "operation": "UPDATE_DETAIL_PICS", "state": "VERIFIED", "metrics": {"broadcast_url_count": 16, "good_detail_pics_url_count": 16}},
            {"key": "FINAL_GOOD_DETAILS_HTML", "operation": "UPDATE_GOOD_DETAILS", "state": "VERIFIED", "metrics": {"broadcast_url_count": 16, "good_detail_pics_url_count": 16}},
        ],
    }
    conclusion = build_final_e2e_conclusion(checkpoint)
    assert conclusion["production_import_architecture_verified"] is True
    checkpoint["status"] = "FROZEN_ON_FIRST_ANOMALY"
    assert build_final_e2e_conclusion(checkpoint)["production_import_architecture_verified"] is False
