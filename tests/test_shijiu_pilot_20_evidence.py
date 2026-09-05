from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def test_checked_in_pilot_is_stale_with_zero_target_writes() -> None:
    checkpoint = load("state/shijiu_pilot_20_batch_checkpoint.json")
    readiness = load("deliverables/shijiu_import/pilot_20_mutex_readiness.json")
    assert checkpoint["status"] == "STALE_BUSINESS_RULE_CHANGED"
    assert checkpoint["must_never_resume"] is True
    assert checkpoint["next_sequence"] == 1
    assert all(row["status"] == "PLANNED" for row in checkpoint["products"].values())
    assert readiness["status"] == "STALE_BUSINESS_RULE_CHANGED"
    assert readiness["production_write_requests_this_preparation_round"] == 0
    assert readiness["external_mutex_evidence_found"] is False
    assert readiness["operator_confirmation_count"] == 0
    assert readiness["operator_must_create_json_manually"] is False
    assert readiness["cross_source_writes"] == 0
    assert readiness["legacy_reference_touched"] is False
    assert readiness["pdf_special_exclusion_count"] == 351


def test_no_product_execution_checkpoint_or_completion_report_exists() -> None:
    product_state = ROOT / "state/shijiu_pilot_20"
    product_reports = ROOT / "deliverables/shijiu_import/pilot_20_products"
    assert not product_state.exists() or not list(product_state.glob("*.json"))
    assert not product_reports.exists() or not list(product_reports.glob("*.json"))
    assert not (ROOT / "deliverables/shijiu_import/pilot_20_completion_report.json").exists()
    assert not (
        ROOT / "deliverables/shijiu_import/remaining_mikihouse_initialization_plan.json"
    ).exists()
