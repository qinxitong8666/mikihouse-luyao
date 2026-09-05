from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "deliverables/shijiu_import"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_no_raw_credentials(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in {"token", "secret", "cookie", "authorization"}:
                assert child in (None, "", "<redacted>")
            _assert_no_raw_credentials(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_raw_credentials(child)


def test_checked_in_richtext_e2e_technical_evidence_and_mutex_fail_closed() -> None:
    checkpoint = load(ROOT / "state/shijiu_richtext_e2e_checkpoint.json")
    report = load(BASE / "richtext_e2e_validation_report.json")
    conclusion = load(BASE / "richtext_e2e_conclusion.json")
    readiness = load(BASE / "richtext_e2e_production_readiness.json")
    mutex = load(BASE / "richtext_e2e_writer_mutex_audit.json")
    mapping = load(ROOT / "state/shijiu_mappings.json")["products"]["10-9332-796"]

    assert checkpoint["status"] == "COMPLETED"
    assert [row["key"] for row in checkpoint["stages"]] == [
        "CREATE_CORE",
        "BROADCAST_5_12",
        "BROADCAST_13_18",
        "DETAIL_PICS_1_8",
        "DETAIL_PICS_9_16",
    ]
    assert all(row["state"] == "VERIFIED" for row in checkpoint["stages"])
    assert report["status"] == "TECHNICALLY_VERIFIED_MUTEX_EVIDENCE_NOT_CAPTURED"
    assert report["technical_five_stage_readback_completed"] is True
    assert report["production_architecture_verified"] is False
    assert conclusion["production_import_architecture_verified"] is False
    assert conclusion["fail_closed_no_further_write"] is True
    assert readiness["status"] == "NOT_READY_MUTEX_EVIDENCE_NOT_CAPTURED"
    assert mutex["concurrent_shijiu_writer_observed"] == "NOT_CAPTURED"
    assert mutex["cross_source_writes"] == 0
    assert mutex["request_counts"] == {
        "create": 1,
        "update": 4,
        "upload": 18,
        "readback": 38,
        "failure": 0,
        "transport_unknown": 0,
    }
    assert mapping["shijiu_product_id"] == "9358340"
    assert len(mapping["variants"]) == 6
    assert all(row["shijiu_sku_id"] is None for row in mapping["variants"].values())


def test_checked_in_media_and_light_details_are_exactly_preserved() -> None:
    checkpoint = load(ROOT / "state/shijiu_richtext_e2e_checkpoint.json")
    preflight = load(BASE / "richtext_e2e_resource_preflight.json")
    readbacks = load(BASE / "richtext_e2e_readbacks.json")
    hashes = {row["metrics"]["good_details_sha256"] for row in checkpoint["stages"]}

    assert preflight["status"] == "PASSED"
    assert preflight["verified_reference_count"] == 18
    assert preflight["shijiu_requests_sent"] == 0
    assert preflight["shijiu_write_requests_sent"] == 0
    assert readbacks["verified_stage_count"] == 5
    assert len(hashes) == 1
    final = checkpoint["stages"][-1]["metrics"]
    assert final["sku_count"] == 6
    assert final["broadcast_url_count"] == 18
    assert final["good_detail_pics_url_count"] == 16
    assert final["good_details_characters"] == 405
    assert final["good_details_image_count"] == 0
    assert final["good_details_url_count"] == 0


def test_historical_next_20_is_stale_and_must_never_execute() -> None:
    plan = load(BASE / "richtext_e2e_next_20_frozen_plan.json")
    special = {
        row.split(",", 1)[0].lstrip("\ufeff")
        for row in (ROOT / "special_skus_2026aw.csv").read_text(
            encoding="utf-8-sig"
        ).splitlines()[1:]
        if row
    }
    assert plan["status"] == "STALE_BUSINESS_RULE_CHANGED"
    assert plan["historical_evidence_only"] is True
    assert plan["must_never_execute"] is True
    assert plan["product_count"] == 20
    assert plan["coverage"]["classification_counts"] == {
        "footwear": 5,
        "apparel": 5,
        "baby": 5,
        "goods": 5,
    }
    assert plan["coverage"]["includes_simple"] is True
    assert plan["coverage"]["includes_multi_sku"] is True
    assert plan["coverage"]["includes_rich_media"] is True
    assert plan["execution_authorized"] is False
    assert plan["real_write_requests"] == 0
    assert plan["fail_closed_no_write"] is True
    assert all(row["required_stages"] for row in plan["products"])
    assert len(special) == 351
    assert not ({row["product_number"] for row in plan["products"]} & special)
    assert plan["legacy_reference_touched"] is False


def test_new_evidence_contains_no_raw_credentials() -> None:
    paths = [
        ROOT / "state/shijiu_richtext_e2e_checkpoint.json",
        *BASE.glob("richtext_e2e_*.json"),
    ]
    for path in paths:
        _assert_no_raw_credentials(load(path))
