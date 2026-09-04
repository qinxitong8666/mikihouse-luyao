from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def test_complete_staged_live_evidence_is_fail_closed_and_internally_consistent() -> None:
    selection = load("config/shijiu_staged_rich_media_complete_single.json")
    checkpoint = load("state/shijiu_staged_rich_media_complete_single_checkpoint.json")
    report = load("deliverables/shijiu_import/staged_rich_media_complete_validation_report.json")
    conclusion = load("deliverables/shijiu_import/staged_rich_media_complete_capacity_conclusion.json")
    readiness = load("deliverables/shijiu_import/staged_rich_media_complete_readiness.json")
    mapping = load("state/shijiu_mappings.json")
    with (ROOT / "special_skus_2026aw.csv").open(encoding="utf-8", newline="") as handle:
        special = {row["product_number"] for row in csv.DictReader(handle)}

    number = selection["product"]["product_number"]
    assert number == "10-9129-792"
    assert len(special) == 351 and number not in special
    assert number not in selection["historical_prohibited_product_numbers"]
    assert selection["product"]["official_image_count"] == 27
    assert checkpoint["resource_preflight"]["status"] == "PASSED"
    assert checkpoint["resource_preflight"]["verified_reference_count"] == 27
    assert checkpoint["resource_preflight"]["shijiu_requests_sent"] == 0
    assert checkpoint["resource_preflight"]["shijiu_write_requests_sent"] == 0

    assert checkpoint["status"] == "FROZEN_ON_FIRST_ANOMALY"
    assert checkpoint["stage_cursor"] == 4
    assert [row["key"] for row in checkpoint["stages"] if row["state"] == "VERIFIED"] == [
        "CREATE_CORE",
        "BROADCAST_5_12",
        "BROADCAST_13_20",
        "BROADCAST_21_27",
    ]
    failed = checkpoint["stages"][4]
    assert failed["key"] == "DETAIL_PICS_1_8"
    assert failed["attempts"] == 0 and "payload_sha256" not in failed
    assert checkpoint["first_failed_state"]["mutation_request_sent"] is False
    assert checkpoint["first_failed_state"]["failure_scope"] == "PRE_UPDATE_READ_ONLY_GATE"
    assert checkpoint["first_failed_state"]["target_state_changed_by_failed_stage"] is False

    ledger = checkpoint["request_ledger"]
    saves = [
        row for row in ledger
        if row.get("path") == "/shopapi/Goods/newAddGood"
        and row.get("semantic_operation") == "write"
    ]
    assert len(saves) == 4
    assert report["request_counts"]["create"] == 1
    assert report["request_counts"]["update"] == 3
    assert conclusion["detail_update_requests_sent"] == 0
    assert conclusion["maximum_verified_broadcast_url_count"] == 27
    assert conclusion["maximum_verified_good_detail_pics_url_count"] == 0
    assert conclusion["final_good_details_html_verified"] is False
    assert conclusion["production_import_architecture_verified"] is False
    assert readiness["next_20_plan_generated"] is False
    assert not (ROOT / "deliverables/shijiu_import/staged_rich_media_next_20_frozen_plan.json").exists()

    current = mapping["products"][number]
    assert current["shijiu_product_id"] == "9358255"
    assert len(current["variants"]) == 3
    assert all(row["shijiu_sku_id"] is None for row in current["variants"].values())
    assert mapping["products"]["10-8375-578"]["shijiu_product_id"] == "9358250"


def test_all_preexisting_frozen_evidence_still_matches_selection_hashes() -> None:
    selection = load("config/shijiu_staged_rich_media_complete_single.json")
    for relative, expected in selection["protected_frozen_evidence"].items():
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected
