from __future__ import annotations

import argparse
import json
from pathlib import Path

from mikihouse_luyao.shijiu_canonical_create import load_verified_browser_credentials
from mikihouse_luyao.shijiu_canonical_reconciliation import (
    load_reconciliation_inputs,
    reconcile_historical_create_read_only,
)
from mikihouse_luyao.shijiu_live_import import ShijiuLiveClient


ROOT = Path(__file__).resolve().parents[1]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Strictly read-only reconciliation for the one historical "
            "36-2001-572 CREATE; never creates, updates, or uploads"
        )
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
    candidate_report_path = (
        ROOT / "deliverables/shijiu_import/canonical_create_candidate.json"
    )
    reconciliation_report_path = (
        ROOT / "deliverables/shijiu_import/canonical_create_reconciliation_report.json"
    )
    item, payload, checkpoint, special = load_reconciliation_inputs(
        ROOT / "output/storefront-master/master_catalog.json",
        ROOT / "special_skus_2026aw.csv",
        mapping_path,
        checkpoint_path,
    )
    token, secret, _ = load_verified_browser_credentials(
        args.browser_private_dir,
        ROOT / "config/shijiu_native_create_contract.json",
    )
    # Deliberately use a non-matching write confirmation. The reconciliation
    # implementation calls only Goods.index and getFormatInfo.
    client = ShijiuLiveClient(token, secret, write_confirmation="READ_ONLY_RECONCILIATION")
    report = reconcile_historical_create_read_only(
        client,
        item,
        payload,
        checkpoint,
        special,
        checkpoint_path=checkpoint_path,
        mapping_path=mapping_path,
        validation_report_path=validation_report_path,
        candidate_report_path=candidate_report_path,
        reconciliation_report_path=reconciliation_report_path,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "RECONCILED_READBACK_VERIFIED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
