from __future__ import annotations

import json
from pathlib import Path

import pytest

from mikihouse_luyao.csv_input import read_product_numbers
from mikihouse_luyao.shijiu_import import load_mapping_state
from mikihouse_luyao.shijiu_live_import import LiveImportError
from mikihouse_luyao.shijiu_pilot_20 import (
    validate_frozen_pilot_plan,
)


ROOT = Path(__file__).resolve().parents[1]


def loaded() -> tuple[dict, set[str], dict]:
    plan = json.loads(
        (ROOT / "deliverables/shijiu_import/richtext_e2e_next_20_frozen_plan.json").read_text()
    )
    special = set(read_product_numbers(ROOT / "special_skus_2026aw.csv"))
    mapping = load_mapping_state(ROOT / "state/shijiu_mappings.json")
    return plan, special, mapping


def test_checked_in_pilot_plan_is_permanently_stale_and_not_executable() -> None:
    plan, special, mapping = loaded()
    assert plan["status"] == "STALE_BUSINESS_RULE_CHANGED"
    assert plan["must_never_execute"] is True
    with pytest.raises(LiveImportError, match="STALE_BUSINESS_RULE_CHANGED"):
        validate_frozen_pilot_plan(plan, special, mapping)
