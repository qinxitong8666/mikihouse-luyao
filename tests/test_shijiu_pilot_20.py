from __future__ import annotations

import json
from pathlib import Path

import pytest

from mikihouse_luyao.csv_input import read_product_numbers
from mikihouse_luyao.shijiu_import import load_category_map, load_mapping_state
from mikihouse_luyao.shijiu_live_import import LiveImportError
from mikihouse_luyao.shijiu_pilot_20 import (
    PILOT_MODE,
    PILOT_PRODUCT_COUNT,
    build_pilot_product_selection,
    initial_pilot_checkpoint,
    validate_frozen_pilot_plan,
    waiting_operator_report,
)


ROOT = Path(__file__).resolve().parents[1]


def loaded() -> tuple[dict, set[str], dict]:
    plan = json.loads(
        (ROOT / "deliverables/shijiu_import/richtext_e2e_next_20_frozen_plan.json").read_text()
    )
    special = set(read_product_numbers(ROOT / "special_skus_2026aw.csv"))
    mapping = load_mapping_state(ROOT / "state/shijiu_mappings.json")
    return plan, special, mapping


def test_checked_in_pilot_plan_is_exactly_twenty_unmapped_non_special_products() -> None:
    plan, special, mapping = loaded()
    rows = validate_frozen_pilot_plan(plan, special, mapping)
    assert len(rows) == PILOT_PRODUCT_COUNT
    assert [row["sequence"] for row in rows] == list(range(1, 21))
    assert not ({row["product_number"] for row in rows} & special)
    assert all(
        mapping["products"][row["product_number"]]["shijiu_product_id"] is None
        for row in rows
    )


def test_each_frozen_product_rebuilds_the_same_payload_and_stage_plan() -> None:
    plan, special, mapping = loaded()
    master_path = ROOT / "output/storefront-master/master_catalog.json"
    if not master_path.exists():
        pytest.skip("formal Storefront master catalog is a protected local production artifact")
    master = json.loads(master_path.read_text())
    category = load_category_map(ROOT / "config/shijiu_category_map.json")
    for row in plan["products"]:
        item, selection = build_pilot_product_selection(
            ROOT, master, special, mapping, category, row
        )
        assert selection["mode"] == PILOT_MODE
        assert selection["product"]["product_number"] == row["product_number"]
        assert selection["product"]["source_payload_sha256"] == row["payload_sha256"]
        assert len(selection["stages"]) == len(row["required_stages"])
        assert item["target_category"]["id"] == 294884
        assert item["publish_ready"] is True


def test_pilot_fails_closed_if_one_product_becomes_mapped() -> None:
    plan, special, mapping = loaded()
    number = plan["products"][0]["product_number"]
    mapping["products"][number]["shijiu_product_id"] = "foreign-or-duplicate"
    with pytest.raises(LiveImportError, match="already mapped"):
        validate_frozen_pilot_plan(plan, special, mapping)
    assert validate_frozen_pilot_plan(
        plan, special, mapping, allow_mapped={number}
    )[0]["product_number"] == number


def test_waiting_checkpoint_and_report_have_zero_writes() -> None:
    plan, _, _ = loaded()
    checkpoint = initial_pilot_checkpoint(plan)
    report = waiting_operator_report(plan, checkpoint)
    assert checkpoint["status"] == "WAITING_OPERATOR_MUTEX_CONFIRMATION"
    assert report["status"] == "WAITING_OPERATOR_MUTEX_CONFIRMATION"
    assert report["product_count"] == 20
    assert report["production_write_requests_this_preparation_round"] == 0
    assert report["external_mutex_evidence_found"] is False
    assert report["operator_confirmation_count"] == 1
    assert report["operator_must_create_json_manually"] is False
    assert report["cross_source_writes"] == 0
