from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "confirm_shijiu_writer_window", ROOT / "scripts/confirm_shijiu_writer_window.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_operator_helper_rejects_stale_historical_plan(monkeypatch) -> None:
    plan = json.loads(
        (ROOT / "deliverables/shijiu_import/richtext_e2e_next_20_frozen_plan.json").read_text()
    )
    monkeypatch.setattr(MODULE, "_head", lambda: "a" * 40)
    with pytest.raises(ValueError, match="not-yet-executed"):
        MODULE.build_evidence(
            plan,
            basis="operator_confirmed_global_window",
            minutes=120,
        )
