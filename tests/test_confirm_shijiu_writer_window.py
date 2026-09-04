from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "confirm_shijiu_writer_window", ROOT / "scripts/confirm_shijiu_writer_window.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_operator_helper_builds_all_scopes_without_raw_confirmation(monkeypatch) -> None:
    plan = json.loads(
        (ROOT / "deliverables/shijiu_import/richtext_e2e_next_20_frozen_plan.json").read_text()
    )
    monkeypatch.setattr(MODULE, "_head", lambda: "a" * 40)
    evidence = MODULE.build_evidence(
        plan,
        basis="operator_confirmed_global_window",
        minutes=120,
    )
    assert evidence["shijiu_writer_source"] == "MIKIHOUSE"
    assert evidence["head_sha"] == "a" * 40
    assert len(evidence["authorized_scopes"]) == 20
    assert evidence["concurrent_shijiu_writer_observed"] is False
    assert evidence["exclusive_window_confirmed"] is True
    assert "我已确认" not in json.dumps(evidence, ensure_ascii=False)
