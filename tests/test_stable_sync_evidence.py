from __future__ import annotations

import gzip
import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(path: str) -> dict:
    target = ROOT / path
    opener = gzip.open if target.suffix == ".gz" else open
    with opener(target, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def test_persisted_source_state_matches_complete_stable_catalog_baseline() -> None:
    state = load("state/mikihouse_source_sync_state.json.gz")
    assert state["source"] == "MIKIHOUSE"
    assert state["target"] == "SHIJIU"
    assert state["last_successful_crawl"]["complete_pagination_validated"] is True
    assert state["last_successful_crawl"]["storefront_product_count"] == 2961
    assert len(state["products"]) == 2961
    statuses = Counter(row["stability_status"] for row in state["products"].values())
    assert statuses == {
        "STABLE": 2435,
        "PDF_SPECIAL_LIST": 311,
        "WEB_EXCLUSIVE": 186,
        "LIMITED_TIME_PRICE": 2,
        "NON_SELLABLE_SERVICE_OR_ADDON": 27,
    }
    assert sum(len(row["variants"]) for row in state["products"].values()) == 18533
    assert state["permanent_exclusions"]["PDF_SPECIAL_LIST"]["count"] == 351
    assert state["permanent_exclusions"]["NON_SELLABLE_SERVICE_OR_ADDON"]["count"] == 27
    for number, product in state["products"].items():
        for sku, variant in product["variants"].items():
            assert variant["source_variant_id"] == f"MIKIHOUSE:{number}:{sku}"


def test_real_offline_replay_and_target_write_safety_are_recorded() -> None:
    report = load("deliverables/storefront_stable_catalog/sync_cycle_planning_report.json")
    readiness = load("deliverables/storefront_stable_catalog/future_automatic_sync_readiness.json")
    assert report["mode"] == "PLANNING_ONLY"
    assert report["stable_catalog_is_only_eligible_source_of_truth"] is True
    assert report["identical_snapshot_replay"] is True
    assert report["idempotent_replay_produced_no_new_events"] is True
    assert report["counts"]["new_event_count"] == 0
    assert report["counts"]["new_action_count"] == 0
    assert report["counts"]["pending_action_event_count"] == 1
    assert report["counts"]["pending_action_counts"] == {"CREATE_PRODUCT": 1}
    assert all(value == 0 for key, value in report["safety"].items() if key.endswith("requests") or key.endswith("writes"))
    assert report["safety"]["legacy_286_touched"] is False
    assert report["safety"]["writer_mutex_evidence_generated"] is False
    assert readiness["production_write_status"] == "PROHIBITED_WAWU_MAY_BE_ACTIVE"
    assert readiness["implementation_boundary"]["current_terminal_stage"] == "PLANNING_ONLY"
    assert readiness["hard_guards"]["special_351_never_emit_shijiu_action"] is True
    assert readiness["hard_guards"][
        "non_sellable_services_never_emit_create_price_inventory_image_or_reactivation_action"
    ] is True
