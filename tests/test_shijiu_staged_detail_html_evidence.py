from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def test_detail_html_live_evidence_is_frozen_and_internally_consistent() -> None:
    selection = load("config/shijiu_staged_detail_html_single.json")
    checkpoint = load("state/shijiu_staged_detail_html_single_checkpoint.json")
    report = load("deliverables/shijiu_import/staged_detail_html_validation_report.json")
    conclusion = load("deliverables/shijiu_import/staged_detail_html_capacity_conclusion.json")
    forensics = load("deliverables/shijiu_import/staged_detail_html_false_negative_forensics.json")
    readiness = load("deliverables/shijiu_import/staged_detail_html_readiness.json")
    mapping = load("state/shijiu_mappings.json")
    with (ROOT / "special_skus_2026aw.csv").open(encoding="utf-8", newline="") as handle:
        special = {row["product_number"] for row in csv.DictReader(handle)}

    number = selection["product"]["product_number"]
    assert number == "10-5292-148"
    assert len(special) == 351 and number not in special
    assert number not in selection["historical_prohibited_product_numbers"]
    assert selection["product"]["name_unique_in_source"] is True
    assert selection["product"]["variant_count"] == 6
    assert selection["product"]["broadcast_count"] == 18
    assert selection["product"]["detail_pic_count"] == 16
    assert selection["product"]["role_counts"]["product_gallery"] > 0
    assert selection["product"]["role_counts"]["detail"] > 0
    assert selection["ui_read_retry_policy"] == {
        "applies_only_to": ["Goods.index", "getFormatInfo"],
        "maximum_retries_after_initial_attempt": 3,
        "initial_backoff_seconds": 0.5,
        "backoff": "exponential",
        "transient_http_status_codes": [502, 503, 504],
        "mutation_retry_count": 0,
    }

    preflight = checkpoint["resource_preflight"]
    assert preflight["status"] == "PASSED"
    assert preflight["verified_reference_count"] == 18
    assert preflight["shijiu_requests_sent"] == 0
    assert preflight["shijiu_write_requests_sent"] == 0
    assert checkpoint["status"] == "FROZEN_ON_FIRST_ANOMALY"
    assert checkpoint["stage_cursor"] == 3
    assert [row["key"] for row in checkpoint["stages"] if row["state"] == "VERIFIED"] == [
        "CREATE_CORE", "BROADCAST_5_12", "BROADCAST_13_18"
    ]
    failed = checkpoint["stages"][3]
    assert failed["key"] == "DETAIL_PICS_1_8"
    assert failed["attempts"] == 1
    assert checkpoint["first_failed_state"]["mutation_request_sent"] is True
    assert checkpoint["stages"][4]["attempts"] == 0
    assert checkpoint["stages"][5]["attempts"] == 0

    assert report["request_counts"] == {
        "read": 393, "write": 22, "image_upload": 18, "create": 1, "update": 3,
    }
    assert report["ui_read_retry_attempt_count"] == 0
    assert report["ui_transient_read_error_count"] == 0
    assert report["mutation_auto_retry_count"] == 0
    assert forensics["status"] == "POST_MUTATION_SNAPSHOT_PASSES_CORRECTED_STAGE_CONTRACT"
    assert forensics["observed_good_detail_pics_count"] == 8
    assert forensics["observed_broadcast_count"] == 18
    assert forensics["observed_sku_count"] == 6
    assert forensics["all_skus_prices_stocks_specs_and_images_verified"] is True
    assert forensics["target_mutations_sent_by_analysis"] == 0
    assert conclusion["minimum_required_good_detail_pics_forensically_verified"] is True
    assert conclusion["final_good_details_html_verified"] is False
    assert conclusion["production_import_architecture_verified"] is False
    assert readiness["next_20_plan_generated"] is False
    assert readiness["next_20_executed"] is False
    assert not (ROOT / "deliverables/shijiu_import/staged_detail_html_next_20_frozen_plan.json").exists()

    mapped = mapping["products"][number]
    assert mapped["shijiu_product_id"] == "9358309"
    assert len(mapped["variants"]) == 6
    assert all(row["shijiu_sku_id"] is None for row in mapped["variants"].values())
    assert mapping["products"]["10-9129-792"]["shijiu_product_id"] == "9358255"


def test_detail_html_run_preserved_every_frozen_evidence_file_and_mapping_row() -> None:
    selection = load("config/shijiu_staged_detail_html_single.json")
    mapping = load("state/shijiu_mappings.json")
    for relative, expected in selection["protected_frozen_evidence"].items():
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected
    for number, expected in selection["protected_existing_mapping_row_hashes"].items():
        serialized = json.dumps(
            mapping["products"][number], ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        assert hashlib.sha256(serialized).hexdigest() == expected
