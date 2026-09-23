#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from mikihouse_luyao.wechat_daily_production import (
    WeChatDailyProductionError,
    audit_completed_daily_favorites_readonly,
)
from mikihouse_luyao.wechat_favorite_runtime import WeChatRuntimeError


ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only audit of today's two completed MIKI HOUSE Favorite titles"
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=ROOT / "outputs" / "daily_quote",
    )
    parser.add_argument(
        "--runtime-config",
        type=Path,
        default=ROOT / "config" / "wechat_favorite_runtime.json",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()
    daily_dir = args.output_root / today
    try:
        config = json.loads(args.runtime_config.read_text(encoding="utf-8"))
        result = audit_completed_daily_favorites_readonly(
            daily_dir,
            config,
            require_today=True,
        )
    except (OSError, ValueError, json.JSONDecodeError, WeChatDailyProductionError, WeChatRuntimeError) as exc:
        print(
            json.dumps(
                {
                    "status": "FAILED_CLOSED",
                    "error": str(exc),
                    "checkpoint_reset_count": 0,
                    "wechat_mutation_count": 0,
                },
                ensure_ascii=False,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "READY_FOR_EXPLICIT_REBUILD_AUTHORIZATION" else 3


if __name__ == "__main__":
    raise SystemExit(main())
