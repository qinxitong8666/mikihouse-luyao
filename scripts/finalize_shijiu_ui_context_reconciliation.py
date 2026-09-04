from __future__ import annotations

import argparse
import json
from pathlib import Path

from mikihouse_luyao.shijiu_canonical_reconciliation import load_reconciliation_inputs
from mikihouse_luyao.shijiu_ui_context_reconciliation import (
    finalize_ui_context_reconciliation,
)


ROOT = Path(__file__).resolve().parents[1]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Finalize the private, zero-mutation Shijiu UI-context reconciliation"
    )
    result.add_argument("--browser-private-dir", type=Path, required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    checkpoint_path = ROOT / "state/shijiu_canonical_create_checkpoint.json"
    mapping_path = ROOT / "state/shijiu_mappings.json"
    validation_report_path = (
        ROOT / "deliverables/shijiu_import/canonical_create_validation_report.json"
    )
    candidate_report_path = ROOT / "deliverables/shijiu_import/canonical_create_candidate.json"
    report_path = (
        ROOT / "deliverables/shijiu_import/canonical_create_ui_context_reconciliation_report.json"
    )
    item, payload, checkpoint, special = load_reconciliation_inputs(
        ROOT / "output/storefront-master/master_catalog.json",
        ROOT / "special_skus_2026aw.csv",
        mapping_path,
        checkpoint_path,
    )
    canonical_contract = json.loads(
        (ROOT / "config/shijiu_native_create_contract.json").read_text(encoding="utf-8")
    )
    report = finalize_ui_context_reconciliation(
        args.browser_private_dir,
        item,
        payload,
        checkpoint,
        special,
        canonical_contract,
        checkpoint_path=checkpoint_path,
        mapping_path=mapping_path,
        validation_report_path=validation_report_path,
        candidate_report_path=candidate_report_path,
        report_path=report_path,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] in {
        "RECONCILED_READBACK_VERIFIED_UI_CONTEXT",
        "HISTORICAL_CREATE_NOT_PERSISTED_CONFIRMED_BY_UI_CONTEXT",
    } else 2


if __name__ == "__main__":
    raise SystemExit(main())
