from __future__ import annotations

import argparse
import json
from pathlib import Path

from mikihouse_luyao.shijiu_live_import import client_from_env
from mikihouse_luyao.shijiu_recovery import (
    RECOVERY_CONFIRMATION,
    FirstProductRecoveryRunner,
    load_recovery_inputs,
)


ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="One-request recovery for MIKIHOUSE 00-1000-028 only"
    )
    parser.add_argument("--target-env-file", type=Path, required=True)
    parser.add_argument("--confirm", default="")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--read-only-only",
        action="store_true",
        help="perform and persist the complete residual scan without issuing a create request",
    )
    mode.add_argument(
        "--post-recovery-forensics",
        action="store_true",
        help="after the sole recovery attempt, run only a terminal read-only residue audit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if (
        not args.read_only_only
        and not args.post_recovery_forensics
        and args.confirm != RECOVERY_CONFIRMATION
    ):
        raise SystemExit("recovery create blocked: exact confirmation phrase missing")
    item, original_record, payload, _, contract = load_recovery_inputs(
        ROOT / "deliverables/shijiu_import/payload_previews.json",
        ROOT / "state/shijiu_first_live_batch_checkpoint.json",
        ROOT / "state/shijiu_mappings.json",
        ROOT / "special_skus_2026aw.csv",
        ROOT / "config/shijiu_native_create_contract.json",
    )
    client = client_from_env(
        args.target_env_file, write_confirmation=RECOVERY_CONFIRMATION
    )
    runner = FirstProductRecoveryRunner(
        client,
        item,
        original_record,
        payload,
        contract,
        ROOT / "state/shijiu_first_product_recovery_checkpoint.json",
        ROOT / "state/shijiu_mappings.json",
        ROOT / "deliverables/shijiu_import/first_product_recovery_report.json",
        ROOT / "deliverables/shijiu_import/first_product_residual_scan.json",
        ROOT / "deliverables/shijiu_import/first_product_recovery_readback.json",
        confirmation=RECOVERY_CONFIRMATION,
    )
    try:
        if args.post_recovery_forensics:
            report = runner.run_post_recovery_forensics(
                ROOT
                / "deliverables/shijiu_import/first_product_recovery_forensics.json"
            )
        elif args.read_only_only:
            report = runner.run_residual_only()
        else:
            report = runner.run()
    except Exception as error:
        print(json.dumps({"status": "STOPPED", "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
