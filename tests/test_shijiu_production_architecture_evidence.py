from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def test_final_e2e_live_evidence_is_fail_closed_and_internally_consistent() -> None:
    selection = load("config/shijiu_production_architecture_verification_single.json")
    checkpoint = load("state/shijiu_production_architecture_verification_checkpoint.json")
    report = load("deliverables/shijiu_import/production_architecture_validation_report.json")
    conclusion = load("deliverables/shijiu_import/production_architecture_conclusion.json")
    forensic = load("deliverables/shijiu_import/production_architecture_final_html_forensics.json")
    readiness = load("deliverables/shijiu_import/production_architecture_readiness.json")
    mapping = load("state/shijiu_mappings.json")
    with (ROOT / "special_skus_2026aw.csv").open(encoding="utf-8", newline="") as handle:
        special = {row["product_number"] for row in csv.DictReader(handle)}

    number = selection["product"]["product_number"]
    assert number == "63-3210-146"
    assert len(special) == 351 and number not in special
    assert "10-5292-148" in selection["historical_prohibited_product_numbers"]
    assert number not in selection["historical_prohibited_product_numbers"]
    assert selection["product"]["name_unique_in_source"] is True
    assert selection["product"]["variant_count"] == 7
    assert selection["product"]["broadcast_count"] == 17
    assert selection["product"]["detail_pic_count"] == 16
    assert selection["minimum_required_verified_detail_pic_count"] == 16

    preflight = checkpoint["resource_preflight"]
    assert preflight["status"] == "PASSED"
    assert preflight["verified_reference_count"] == 17
    assert preflight["shijiu_requests_sent"] == 0
    assert preflight["shijiu_write_requests_sent"] == 0
    assert all(row["status"] == "VERIFIED" for row in preflight["results"].values())

    assert checkpoint["status"] == "FROZEN_ON_FIRST_ANOMALY"
    assert checkpoint["stage_cursor"] == 5
    assert [row["key"] for row in checkpoint["stages"] if row["state"] == "VERIFIED"] == [
        "CREATE_CORE",
        "BROADCAST_5_12",
        "BROADCAST_13_17",
        "DETAIL_PICS_1_8",
        "DETAIL_PICS_9_16",
    ]
    failed = checkpoint["stages"][5]
    assert failed["key"] == "FINAL_GOOD_DETAILS_HTML"
    assert failed["attempts"] == 1
    assert checkpoint["first_failed_state"]["mutation_request_sent"] is True
    assert checkpoint["first_failed_state"]["failure_phase"] == "POST_MUTATION_UI_CONTEXT_STRONG_READBACK"

    assert report["request_counts"] == {
        "read": 49,
        "write": 23,
        "image_upload": 17,
        "create": 1,
        "update": 5,
    }
    assert report["ui_read_retry_attempt_count"] == 0
    assert report["ui_transient_read_error_count"] == 0
    assert report["mutation_auto_retry_count"] == 0

    assert forensic["status"] == "FINAL_HTML_MUTATION_ACKNOWLEDGED_BUT_TARGET_RETAINED_PRIOR_MINIMAL_HTML"
    assert forensic["mutation_response_code"] == 200
    assert forensic["matching_final_html_mutation_request_count"] == 1
    assert forensic["mutation_was_not_retried"] is True
    assert forensic["observed_matches_previous_minimal_good_details"] is True
    assert forensic["expected_html_cos_image_count"] == 16
    assert forensic["observed_html_image_count"] == 0
    assert forensic["expected_html_all_urls_are_uploaded_targets"] is True
    assert forensic["expected_html_contains_source_hotlinks"] is False
    assert forensic["observed_broadcast_count"] == 17
    assert forensic["observed_good_detail_pics_count"] == 16
    assert forensic["observed_sku_count"] == 7
    assert forensic["all_non_html_fields_match_last_verified_state"] is True
    assert forensic["target_mutations_sent_by_analysis"] == 0

    assert conclusion["minimum_required_good_detail_pics_satisfied"] is True
    assert conclusion["final_good_details_html_verified"] is False
    assert conclusion["production_import_architecture_verified"] is False
    assert readiness["next_20_plan_generated"] is False
    assert readiness["next_20_executed"] is False
    assert not (ROOT / "deliverables/shijiu_import/production_architecture_next_20_frozen_plan.json").exists()

    mapped = mapping["products"][number]
    assert mapped["shijiu_product_id"] == "9358329"
    assert len(mapped["variants"]) == 7
    assert all(row["shijiu_sku_id"] is None for row in mapped["variants"].values())
    assert mapping["products"]["10-5292-148"]["shijiu_product_id"] == "9358309"


def test_final_e2e_preserved_every_historical_evidence_file_and_mapping_row() -> None:
    selection = load("config/shijiu_production_architecture_verification_single.json")
    mapping = load("state/shijiu_mappings.json")
    for relative, expected in selection["protected_frozen_evidence"].items():
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected
    for number, expected in selection["protected_existing_mapping_row_hashes"].items():
        serialized = json.dumps(
            mapping["products"][number], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        assert hashlib.sha256(serialized).hexdigest() == expected
