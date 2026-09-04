from __future__ import annotations

import json
from pathlib import Path

import pytest

from mikihouse_luyao.shijiu_import import (
    ReadOnlyShijiuClient,
    WriteProhibitedError,
    backend_sku_code,
    build_incremental_sync_operations,
    build_contract_audit,
    build_parser,
    choose_action,
    discover_exact_mikihouse_bindings,
    discover_shijiu_read_contract,
    load_mapping_state,
    map_product_to_shijiu,
    plan_import,
    reconcile_mapping_state,
    source_product_id,
    source_variant_id,
    validate_live_mikihouse_category,
)


CATEGORY = {
    "id": 294884,
    "name": "MikiHouse",
    "parent_id": 288338,
    "parent_name": "母婴用品",
    "assignment_policy": "all_publishable_mikihouse_products",
}

PRICE_GUARD = {
    "minimum_tax_included_price_jpy": 1,
    "maximum_tax_included_price_jpy": 1_000_000,
    "maximum_absolute_change_jpy": 50_000,
    "maximum_relative_change_ratio": 0.5,
}


def product(number: str = "20-0001-001", image: bool = True) -> dict:
    picture = {"url": "https://cdn/main.jpg", "width": 700, "height": 700, "alt_text": ""} if image else None
    return {
        "product_number": number,
        "handle": number,
        "name": "ベビーシューズ",
        "brand": "ミキハウス",
        "product_type": "通常商品",
        "category": {"id": "source", "name": "Baby Shoes"},
        "tags": ["shoes"],
        "main_image": picture,
        "color_images": ([{"color": "赤", "images": [{"image": picture}]}] if image else []),
        "product_url": f"https://www.mikihouse.co.jp/products/{number}",
        "active": True,
        "last_seen_at": "2026-09-04T00:00:00+00:00",
        "variants": [
            {
                "stable_id": f"{number}::sku-1",
                "sku": "sku-1",
                "title": "赤 / 12.5cm",
                "active": True,
                "available_for_sale": True,
                "selected_options": [
                    {"name": "カラー", "value": "赤"},
                    {"name": "サイズ", "value": "12.5cm"},
                ],
                "color": "赤",
                "size": "12.5cm",
                "tax_included_price_jpy": 11_001,
                "mini_program_price_jpy": 7_151,
                "resolved_image": picture,
            }
        ],
    }


def test_stable_source_ids_and_backend_sku_code() -> None:
    assert source_product_id("20-0001-001") == "MIKIHOUSE:20-0001-001"
    assert source_variant_id("20-0001-001", "sku-1") == "MIKIHOUSE:20-0001-001:sku-1"
    assert backend_sku_code("sku-1") == "MIKI-sku-1"


def test_mapper_copies_existing_jpy_price_and_all_variant_fields() -> None:
    mapped = map_product_to_shijiu(product(), CATEGORY)
    sku = mapped["shijiu_payload_preview"]["sku_info"][0]
    source = mapped["source_variants"][0]
    assert mapped["currency"] == "JPY"
    assert mapped["currency_conversion_applied"] is False
    assert mapped["target_category"] == CATEGORY
    assert mapped["shijiu_payload_preview"]["good_type"] == 294884
    assert mapped["source_product_type"] == "通常商品"
    assert mapped["source_tags"] == ["shoes"]
    assert mapped["source_brand"] == "ミキハウス"
    assert mapped["shijiu_payload_preview"]["brand_id"] == ""
    assert mapped["shijiu_payload_preview"]["supplier"] == ""
    assert mapped["publish_ready"] is True
    assert sku["sku_price"] == "7151.00"
    assert sku["sku_cost_price"] == "11001.00"
    assert sku["sku_stock"] == "1.00"
    assert sku["sku_code"] == "MIKI-sku-1"
    assert sku["spec_name"] == "赤,12.5cm"
    assert sku["sku_thumbnail"] == "https://cdn/main.jpg"
    assert source["color"] == "赤" and source["size"] == "12.5cm"
    assert source["stock_source"] == "storefront_availableForSale_boolean"


def test_missing_image_is_unpublishable_and_skipped() -> None:
    mapped = map_product_to_shijiu(product(image=False), CATEGORY)
    assert mapped["publish_ready"] is False
    assert mapped["publish_blockers"] == ["missing_official_image"]
    assert choose_action(mapped, None, None) == ("skip", "missing_official_image")


def test_read_only_client_blocks_every_non_allowlisted_endpoint_before_network() -> None:
    client = ReadOnlyShijiuClient("token", "secret")
    for path in (
        "/shopapi/Goods/newAddGood",
        "/shopapi/Goods/grounding",
        "/shopapi/Goods/delGood",
        "/v1/cos/upload",
    ):
        with pytest.raises(WriteProhibitedError):
            client.request_read(path, {})
    assert client.requests == []
    assert client.semantic_write_request_count == 0


def test_live_category_mapping_must_match_id_and_name() -> None:
    response = {"data": [{
        "id": 288338,
        "type_name": "母婴用品",
        "pid": 0,
        "children": [{"id": 294884, "type_name": "MikiHouse", "pid": 288338, "children": []}],
    }]}
    result = validate_live_mikihouse_category(CATEGORY, response)
    assert result["passed"] is True
    assert result["all_products_fixed_to_category_id"] == 294884
    response["data"][0]["children"][0]["id"] = 123
    with pytest.raises(Exception, match="category validation"):
        validate_live_mikihouse_category(CATEGORY, response)


def test_checkpoint_resume_reuses_planned_items_without_duplicates(tmp_path: Path) -> None:
    products = [product(f"20-0001-00{i}") for i in range(1, 4)]
    changes = {
        "is_initial_sync": True,
        "changes": [],
    }
    mapping_state = reconcile_mapping_state(load_mapping_state(tmp_path / "missing.json"), products, CATEGORY)
    checkpoint = tmp_path / "checkpoint.json"
    first, first_summary = plan_import(
        products,
        changes,
        CATEGORY,
        mapping_state,
        {},
        checkpoint,
        max_items=2,
    )
    assert len(first) == 2
    assert first_summary["complete"] is False

    resumed, resumed_summary = plan_import(
        products,
        changes,
        CATEGORY,
        mapping_state,
        {},
        checkpoint,
        resume=True,
    )
    assert len(resumed) == 3
    assert len({item["source_product_id"] for item in resumed}) == 3
    assert resumed_summary["complete"] is True
    assert resumed_summary["processed_this_run"] == 1
    stored = json.loads(checkpoint.read_text())
    assert stored["remaining_items"] == 0
    assert all("mapped_product" not in item for item in stored["records"].values())


def test_action_selection_uses_stable_mapping_and_target_discovery() -> None:
    mapped = map_product_to_shijiu(product(), CATEGORY)
    assert choose_action(mapped, None, None)[0] == "create"
    mapping = {"backend_product_id": "123", "last_payload_sha256": mapped["payload_sha256"]}
    assert choose_action(mapped, mapping, None)[0] == "skip"
    mapping["last_payload_sha256"] = "changed"
    assert choose_action(mapped, mapping, None)[0] == "update"
    mapping["target_active"] = False
    assert choose_action(mapped, mapping, None)[0] == "reactivate"
    assert choose_action(mapped, None, {"matched": True})[0] == "update"

    inactive_product = product()
    inactive_product["active"] = False
    inactive = map_product_to_shijiu(inactive_product, CATEGORY)
    assert choose_action(inactive, mapping, None)[0] == "deactivate"


def test_cli_has_no_write_or_confirm_write_option() -> None:
    destinations = {action.dest for action in build_parser()._actions}
    assert "write" not in destinations
    assert "confirm_write" not in destinations
    assert "dry_run" not in destinations


def test_contract_audit_is_truthful_about_reference_live_write_evidence() -> None:
    audit = build_contract_audit()
    assert audit["target"] == "SHIJIU"
    assert any("zero writes" in item for item in audit["missing_evidence"])
    assert "WAWU mapper and source field semantics" in audit["excluded_reference_components"]


def test_read_contract_discovery_records_schema_not_business_values() -> None:
    class FakeClient:
        def search_products(self, **_kwargs):
            return {"count": 1, "data": [{"id": 99, "good_name": "private-name"}]}

        def product_detail(self, backend_product_id):
            assert backend_product_id == 99
            return {"data": {"sku_info": [{"sku_code": "private-sku", "sku_price": "1.00"}]}}

    result = discover_shijiu_read_contract(FakeClient())
    assert result["passed"] is True
    assert result["backend_product_id_observed"] is True
    assert result["list_row_keys"] == ["good_name", "id"]
    assert result["detail_sku_field_keys"] == ["sku_code", "sku_price"]
    assert "private-name" not in json.dumps(result)
    assert "private-sku" not in json.dumps(result)


def test_mapping_state_creates_isolated_rows_for_every_mikihouse_identity(tmp_path: Path) -> None:
    products = [product("20-0001-001"), product("20-0001-002")]
    state = reconcile_mapping_state(load_mapping_state(tmp_path / "missing.json"), products, CATEGORY)
    assert state["source"] == "MIKIHOUSE" and state["target"] == "SHIJIU"
    assert set(state["products"]) == {"20-0001-001", "20-0001-002"}
    row = state["products"]["20-0001-001"]
    assert row["source_product_id"] == "MIKIHOUSE:20-0001-001"
    assert row["shijiu_product_id"] is None
    assert row["variants"]["sku-1"]["source_variant_id"] == "MIKIHOUSE:20-0001-001:sku-1"
    assert row["variants"]["sku-1"]["shijiu_sku_id"] is None
    assert state["identity_contract"]["product_name_matching"] == "forbidden"


def test_cross_provider_mapping_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "source": "MIKIHOUSE",
        "target": "SHIJIU",
        "identity_contract": {"product_name_matching": "forbidden"},
        "products": {"20-0001-001": {"source_product_id": "WAWU:20-0001-001", "variants": {}}},
    }))
    with pytest.raises(Exception, match="cross-provider"):
        load_mapping_state(path)


def test_price_changed_only_updates_exact_bound_sku_and_abnormal_change_is_reviewed(tmp_path: Path) -> None:
    item = product()
    state = reconcile_mapping_state(load_mapping_state(tmp_path / "missing.json"), [item], CATEGORY)
    state["products"][item["product_number"]]["shijiu_product_id"] = "9001"
    changes = {"is_initial_sync": False, "changes": [{
        "detected_at": "2026-09-04T00:00:00Z",
        "change_type": "price_changed",
        "product_number": item["product_number"],
        "variant_sku": "sku-1",
        "before": {"tax_included_price_jpy": 10_000, "mini_program_price_jpy": 6_500},
        "after": {"tax_included_price_jpy": 11_001, "mini_program_price_jpy": 7_151},
    }, {
        "detected_at": "2026-09-04T00:00:00Z",
        "change_type": "inventory_changed",
        "product_number": item["product_number"],
        "variant_sku": "sku-1",
        "before": True,
        "after": False,
    }]}
    mapped = {item["product_number"]: map_product_to_shijiu(item, CATEGORY)}
    operations, reviews = build_incremental_sync_operations(changes, state, mapped, PRICE_GUARD)
    assert operations[0]["change_type"] == "PRICE_CHANGED"
    assert operations[0]["planned_action"] == "UPDATE_PRICE_BY_EXACT_VARIANT_SKU"
    assert operations[0]["price_change"]["after_mini_program_price_jpy"] == 7151
    assert operations[0]["currency_conversion_applied"] is False
    assert operations[1]["change_type"] == "INVENTORY_CHANGED"
    assert operations[1]["planned_action"] == "UPDATE_INVENTORY"
    assert reviews == []

    changes["changes"] = [dict(changes["changes"][0], after={
        "tax_included_price_jpy": 100_000,
        "mini_program_price_jpy": 65_000,
    })]
    operations, reviews = build_incremental_sync_operations(changes, state, mapped, PRICE_GUARD)
    assert operations[0]["planned_action"] == "REVIEW_REQUIRED"
    assert reviews and "relative_price_change_exceeds_threshold" in reviews[0]["price_change"]["reasons"]


def test_non_price_change_never_generates_price_update(tmp_path: Path) -> None:
    item = product()
    state = reconcile_mapping_state(load_mapping_state(tmp_path / "missing.json"), [item], CATEGORY)
    changes = {"is_initial_sync": False, "changes": [{
        "change_type": "variant_image_changed",
        "product_number": item["product_number"],
        "variant_sku": "sku-1",
    }]}
    operations, _ = build_incremental_sync_operations(
        changes, state, {item["product_number"]: map_product_to_shijiu(item, CATEGORY)}, PRICE_GUARD
    )
    assert operations[0]["change_type"] == "IMAGE_CHANGED"
    assert "PRICE" not in operations[0]["planned_action"]


def test_unmapped_price_change_never_recreates_product_and_unsafe_create_is_blocked(tmp_path: Path) -> None:
    item = product()
    state = reconcile_mapping_state(load_mapping_state(tmp_path / "missing.json"), [item], CATEGORY)
    mapped = {item["product_number"]: map_product_to_shijiu(item, CATEGORY)}
    changes = {"is_initial_sync": False, "changes": [{
        "change_type": "price_changed",
        "product_number": item["product_number"],
        "variant_sku": "sku-1",
        "before": {"tax_included_price_jpy": 10_000, "mini_program_price_jpy": 6_500},
        "after": {"tax_included_price_jpy": 11_001, "mini_program_price_jpy": 7_151},
    }, {
        "change_type": "new_product",
        "product_number": item["product_number"],
        "variant_sku": None,
    }]}
    operations, _ = build_incremental_sync_operations(
        changes, state, mapped, PRICE_GUARD, automatic_create_allowed=False
    )
    assert operations[0]["planned_action"] == "BLOCKED_UNMAPPED_PRICE_UPDATE"
    assert operations[1]["planned_action"] == "BLOCKED_EXISTING_UNMAPPED_TARGET_PRODUCTS"
    assert all(item["planned_action"] != "CREATE_PRODUCT" for item in operations)


def test_existing_binding_discovery_uses_exact_miki_sku_and_never_name(tmp_path: Path) -> None:
    item = product()
    state = reconcile_mapping_state(load_mapping_state(tmp_path / "missing.json"), [item], CATEGORY)

    class FakeClient:
        def search_products(self, **kwargs):
            assert kwargs["good_type"] == 294884
            return {"count": 2, "data": [{"id": 10}, {"id": 11}]}

        def product_detail(self, backend_product_id):
            code = "MIKI-sku-1" if backend_product_id == 10 else "WAWU-sku-1"
            return {"data": {"sku_info": [{"sku_code": code, "sku_price": "1.00", "id": backend_product_id * 10}]}}

    discovered, report = discover_exact_mikihouse_bindings(FakeClient(), [item], state, CATEGORY)
    mapping = discovered["products"][item["product_number"]]
    assert mapping["shijiu_product_id"] == "10"
    assert mapping["variants"]["sku-1"]["shijiu_sku_id"] == "100"
    assert mapping["match_method"] == "exact_backend_sku_code"
    assert report["foreign_wawu_namespace_codes_observed"] == 1
    assert report["product_name_matching_attempts"] == 0
