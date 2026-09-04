from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "deliverables" / "shijiu_import" / "dry_run_report.json"
SNAPSHOT = ROOT / "deliverables" / "shijiu_import" / "read_only_target_verification.json"
ACTIONS = ROOT / "deliverables" / "shijiu_import" / "dry_run_actions.json"


def test_tracked_shijiu_dry_run_report_is_complete_and_write_free() -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    assert report["target"] == "SHIJIU"
    assert report["source_catalog"]["product_count"] == 2603
    assert report["source_catalog"]["variant_count"] == 15735
    assert report["source_catalog"]["permanent_special_exclusion_count"] == 351
    assert report["source_catalog"]["special_products_in_source"] == []
    assert report["plan_summary"] == {
        "total": 2603,
        "create": 2596,
        "update": 0,
        "deactivate": 0,
        "reactivate": 0,
        "skip": 7,
        "failed": 0,
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
    assert report["write_safety"]["write_capability_present"] is False
    assert report["passed"] is True


def test_tracked_shijiu_request_ledger_contains_only_allowlisted_reads_and_no_credentials() -> None:
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    allowed = {
        "/shopapi/Goods/index",
        "/shopapi/goods/getFormatInfo",
        "/shopapi/goodtype/fatherIndex",
    }
    assert snapshot["read_contract_discovery"]["passed"] is True
    assert snapshot["semantic_write_request_count"] == 0
    assert snapshot["mutating_endpoints_called"] == []
    assert snapshot["read_request_count"] >= 23
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
    counts = {name: sum(item["action"] == name for item in actions) for name in ("create", "skip", "failed")}
    assert counts == {"create": 2596, "skip": 7, "failed": 0}
