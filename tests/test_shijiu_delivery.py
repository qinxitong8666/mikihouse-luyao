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
LEGACY_AUDIT = ROOT / "deliverables" / "shijiu_import" / "legacy_reference_audit.json"
CLEANUP = ROOT / "deliverables" / "shijiu_import" / "legacy_cleanup_plan.json"
PREVIEWS = ROOT / "deliverables" / "shijiu_import" / "payload_previews.json"
EXCLUSIONS = ROOT / "deliverables" / "shijiu_import" / "special_exclusion_report.json"


def test_tracked_shijiu_dry_run_report_is_complete_and_write_free() -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    assert report["target"] == "SHIJIU"
    assert report["source_catalog"]["product_count"] == 2615
    assert report["source_catalog"]["variant_count"] == 15885
    assert report["source_catalog"]["permanent_special_exclusion_count"] == 351
    assert report["source_catalog"]["special_products_in_source"] == []
    assert report["source_catalog"]["declared_baseline_candidate_count"] == 2603
    assert report["source_catalog"]["new_non_special_products_since_declared_baseline"] == 12
    assert report["plan_summary"]["total"] == 2615
    assert report["plan_summary"]["create"] == 2608
    assert report["plan_summary"]["skip"] == 7
    assert report["plan_summary"]["review_required"] == 0
    assert report["plan_summary"]["failed"] == 0
    assert report["price_validation"]["failure_count"] == 0
    assert report["price_validation"]["currency"] == "JPY"
    assert report["price_validation"]["currency_conversion_applied"] is False
    assert report["payload_preview_count"] == 20
    assert report["payload_preview_type_counts"] == {
        "apparel": 5,
        "baby": 5,
        "footwear": 5,
        "goods": 5,
    }
    assert report["target_read_only_verification"]["semantic_write_request_count"] == 0
    assert report["target_read_only_verification"]["fixed_category_discovery"]["configured"]["id"] == 294884
    assert report["target_read_only_verification"]["identity_reconciliation_attempted"] is False
    assert report["target_read_only_verification"]["legacy_product_count"] == 286
    assert report["target_read_only_verification"]["legacy_detail_sample_count"] == 6
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
    assert snapshot["legacy_reference_audit"]["passed"] is True
    assert snapshot["semantic_write_request_count"] == 0
    assert snapshot["mutating_endpoints_called"] == []
    assert snapshot["read_request_count"] == 9
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
    assert len(actions) == 2615
    assert len({item["source_product_id"] for item in actions}) == 2615
    counts = {
        name: sum(item["action"] == name for item in actions)
        for name in ("create", "review_required", "skip", "failed")
    }
    assert counts == {"create": 2608, "review_required": 0, "skip": 7, "failed": 0}


def test_persistent_mapping_table_is_complete_and_provider_isolated() -> None:
    payload = json.loads(MAPPINGS.read_text(encoding="utf-8"))
    assert payload["source"] == "MIKIHOUSE" and payload["target"] == "SHIJIU"
    assert payload["identity_contract"]["product_name_matching"] == "forbidden"
    assert payload["identity_contract"]["legacy_reference_binding"] == "forbidden"
    products = payload["products"]
    variants = [variant for product in products.values() for variant in product["variants"].values()]
    assert len(products) == 2615
    assert len(variants) == 15885
    assert all(item["source_product_id"].startswith("MIKIHOUSE:") for item in products.values())
    assert all(item["source_variant_id"].startswith("MIKIHOUSE:") for item in variants)
    assert not any("WAWU:" in json.dumps(item) for item in products.values())


def test_incremental_plan_separates_change_types_and_blocks_unsafe_writes() -> None:
    summary = json.loads(INCREMENTAL.read_text(encoding="utf-8"))
    assert summary["change_type_counts"] == {"INVENTORY_CHANGED": 1}
    assert summary["planned_action_counts"] == {"BLOCKED_UNMAPPED": 1}
    assert summary["price_changed_count"] == 0
    assert summary["price_update_count"] == 0
    assert summary["price_update_recreates_product"] is False
    assert summary["currency"] == "JPY"
    assert summary["currency_conversion_applied"] is False
    review = json.loads(REVIEW.read_text(encoding="utf-8"))
    assert review["price_review_required"] == []
    assert review["write_executed"] is False


def test_legacy_cleanup_and_payload_previews_are_separate_and_non_executable() -> None:
    audit = json.loads(LEGACY_AUDIT.read_text(encoding="utf-8"))
    cleanup = json.loads(CLEANUP.read_text(encoding="utf-8"))
    previews = json.loads(PREVIEWS.read_text(encoding="utf-8"))
    assert audit["classification"] == "legacy_reference_only"
    assert audit["legacy_product_count"] == 286 and audit["sample_size"] == 6
    assert audit["identity_reconciliation_attempted"] is False
    assert cleanup["action_count"] == 286 and cleanup["write_executed"] is False
    assert all(item["planned_action"] == "OFF_SHELF" for item in cleanup["actions"])
    assert previews["cross_category_product_count"] == 20
    assert previews["classification_counts"] == {
        "apparel": 5, "baby": 5, "footwear": 5, "goods": 5,
    }
    for mapped in previews["payloads"]:
        payload = mapped["shijiu_payload_preview"]
        assert mapped["payload_ready_for_write"] is False
        image_fields = [payload["master_graph"], payload["broadcast"], payload["good_detail_pics"]]
        image_fields.extend(item["sku_thumbnail"] for item in payload["sku_info"])
        assert not any("https://" in value or "http://" in value for value in image_fields)


def test_all_351_pdf_special_products_are_tracked_as_permanent_exclusions() -> None:
    report = json.loads(EXCLUSIONS.read_text(encoding="utf-8"))
    assert report["excluded_reason"] == "PDF_SPECIAL_LIST"
    assert report["total_count"] == 351
    assert report["online_excluded_count"] == 311
    assert report["offline_remembered_count"] == 40
    assert len(report["product_numbers"]) == len(set(report["product_numbers"])) == 351
    assert report["special_products_in_shijiu_candidate_pool"] == []
