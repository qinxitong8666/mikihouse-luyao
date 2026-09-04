from __future__ import annotations

import argparse
import json
from pathlib import Path

from mikihouse_luyao.shijiu_import import load_category_map
from mikihouse_luyao.shijiu_live_import import (
    LIVE_WRITE_CONFIRMATION,
    FirstLiveBatchRunner,
    client_from_env,
    load_live_batch,
)


ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail-closed real import of the frozen first 20 MIKIHOUSE products into Shijiu"
    )
    parser.add_argument("--target-env-file", type=Path, required=True)
    parser.add_argument("--confirm", required=True, help="must equal the documented exact confirmation phrase")
    parser.add_argument(
        "--checkpoint", type=Path,
        default=ROOT / "state" / "shijiu_first_live_batch_checkpoint.json",
    )
    parser.add_argument(
        "--mapping-state", type=Path, default=ROOT / "state" / "shijiu_mappings.json"
    )
    parser.add_argument(
        "--report", type=Path,
        default=ROOT / "deliverables" / "shijiu_import" / "first_live_batch_report.json",
    )
    parser.add_argument(
        "--readbacks", type=Path,
        default=ROOT / "deliverables" / "shijiu_import" / "first_live_batch_readbacks.json",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.confirm != LIVE_WRITE_CONFIRMATION:
        raise SystemExit("real write blocked: incorrect --confirm value")
    items, special, _ = load_live_batch(
        ROOT / "config" / "shijiu_first_live_batch.json",
        ROOT / "deliverables" / "shijiu_import" / "payload_previews.json",
        ROOT / "special_skus_2026aw.csv",
    )
    category = load_category_map(ROOT / "config" / "shijiu_category_map.json")
    client = client_from_env(args.target_env_file)
    runner = FirstLiveBatchRunner(
        client, items, special, category, args.checkpoint, args.mapping_state,
        args.report, args.readbacks, confirmation=args.confirm,
    )
    try:
        report = runner.run()
    except Exception as error:
        print(json.dumps({"status": "STOPPED_ON_FIRST_ERROR", "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
