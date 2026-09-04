from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "deliverables" / "shijiu_import" / "dry_run_report.json"
SNAPSHOT = ROOT / "deliverables" / "shijiu_import" / "read_only_target_verification.json"
ACTIONS = ROOT / "deliverables" / "shijiu_import" / "dry_run_actions.json"
MAPPINGS = ROOT / "state" / "shijiu_mappings.json"
INCREMENTAL = ROOT / "deliverables" / "shijiu_import" / "incremental_sync_summary.json"
REVIEW = ROOT / "deliverables" / "shijiu_import" / "review_required.json"


def test_tracked_shijiu_dry_run_report_is_complete_and_write_free() -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    assert report["target"] == "SHIJIU"
    assert report["source_catalog"]["product_count"] == 2603
    assert report["source_catalog"]["variant_count"] == 15735
    assert report["source_catalog"]["permanent_special_exclusion_count"] == 351
    assert report["source_catalog"]["special_products_in_source"] == []
    assert report["plan_summary"] == {
        "total": 2603,
        "create": 0,
        "update": 0,
        "deactivate": 0,
        "reactivate": 0,
        "skip": 7,
        "failed": 0,
        "review_required": 2596,
        "incremental_change_set": 0,
        "publish_ready": 2596,
        "unpublishable_missing_image": 7,
    }
    assert report["price_validation"]["failure_count"] == 0
    assert report["price_validation"]["currency"] == "JPY"
    assert report["price_validation"]["currency_conversion_applied"] is False
    assert report["target_read_only_verification"]["sample_product_count"] == 20
    assert report["target_read_only_verification"]["type_counts"] == {
        "apparel": 5,
        "baby": 5,
        "footwear": 5,
        "goods": 5,
    }
    assert report["target_read_only_verification"]["semantic_write_request_count"] == 0
    assert report["target_read_only_verification"]["fixed_category_discovery"]["configured"]["id"] == 294884
    assert report["target_read_only_verification"]["exact_binding_discovery"]["product_name_matching_attempts"] == 0
    assert report["target_read_only_verification"]["exact_binding_discovery"]["automatic_create_allowed"] is False
    assert report["provider_isolation"]["shared_product_identity"] is False
    assert report["readiness"]["dry_run_validation_passed"] is True
    assert report["readiness"]["ready_for_online_write"] is False
    assert report["readiness"]["online_write_authorized"] is False
    assert report["write_safety"]["write_capability_present"] is False
    assert report["passed"] is True


def test_tracked_shijiu_request_ledger_contains_only_allowlisted_reads_and_no_credentials() -> None:
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    allowed = {
        "/shopapi/Goods/index",
        "/shopapi/goods/getFormatInfo",
        "/shopapi/Goodtype/typeindex",
        "/shopapi/Goodtype/index",
        "/shopapi/goodtype/fatherIndex",
    }
    assert snapshot["read_contract_discovery"]["passed"] is True
    assert snapshot["semantic_write_request_count"] == 0
    assert snapshot["mutating_endpoints_called"] == []
    assert snapshot["read_request_count"] >= 311
    assert {row["path"] for row in snapshot["request_ledger"]} <= allowed
    serialized = json.dumps(snapshot).casefold()
    assert '"token"' not in serialized
    assert '"secret"' not in serialized
    assert "authorization" not in serialized
    assert "cookie" not in serialized


def test_tracked_action_plan_contains_every_product_once() -> None:
    payload = json.loads(ACTIONS.read_text(encoding="utf-8"))
    assert payload["mode"] == "dry-run"
    assert payload["target"] == "SHIJIU"
    actions = payload["actions"]
    assert len(actions) == 2603
    assert len({item["source_product_id"] for item in actions}) == 2603
    counts = {
        name: sum(item["action"] == name for item in actions)
        for name in ("create", "review_required", "skip", "failed")
    }
    assert counts == {"create": 0, "review_required": 2596, "skip": 7, "failed": 0}


def test_persistent_mapping_table_is_complete_and_provider_isolated() -> None:
    payload = json.loads(MAPPINGS.read_text(encoding="utf-8"))
    assert payload["source"] == "MIKIHOUSE" and payload["target"] == "SHIJIU"
    assert payload["identity_contract"]["product_name_matching"] == "forbidden"
    products = payload["products"]
    variants = [variant for product in products.values() for variant in product["variants"].values()]
    assert len(products) == 2603
    assert len(variants) == 15735
    assert all(item["source_product_id"].startswith("MIKIHOUSE:") for item in products.values())
    assert all(item["source_variant_id"].startswith("MIKIHOUSE:") for item in variants)
    assert not any("WAWU:" in json.dumps(item) for item in products.values())


def test_incremental_plan_separates_change_types_and_blocks_unsafe_writes() -> None:
    summary = json.loads(INCREMENTAL.read_text(encoding="utf-8"))
    assert summary["change_type_counts"] == {"NEW_PRODUCT": 2603, "NEW_VARIANT": 15735}
    assert summary["planned_action_counts"] == {
        "BLOCKED_EXISTING_UNMAPPED_TARGET_PRODUCTS": 18324,
        "SKIP_MISSING_OFFICIAL_IMAGE": 14,
    }
    assert summary["price_changed_count"] == 0
    assert summary["price_update_count"] == 0
    assert summary["price_update_recreates_product"] is False
    assert summary["currency"] == "JPY"
    assert summary["currency_conversion_applied"] is False
    review = json.loads(REVIEW.read_text(encoding="utf-8"))
    assert review["price_review_required"] == []
    assert review["binding_review_required"][0]["unresolved_target_product_count"] == 286
    assert review["write_executed"] is False
