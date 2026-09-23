#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from mikihouse_luyao.wechat_daily_production import (
    WeChatDailyProductionError,
    audit_frozen_pdf_recovery_readonly,
)
from mikihouse_luyao.wechat_favorite_runtime import WeChatRuntimeError


ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only preflight for one frozen PDF attachment recovery"
    )
    parser.add_argument(
        "--output-root", type=Path, default=ROOT / "outputs" / "daily_quote"
    )
    parser.add_argument(
        "--runtime-config",
        type=Path,
        default=ROOT / "config" / "wechat_favorite_runtime.json",
    )
    args = parser.parse_args(argv)
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()
    try:
        runtime_config = json.loads(args.runtime_config.read_text(encoding="utf-8"))
        result = audit_frozen_pdf_recovery_readonly(
            args.output_root / today,
            runtime_config,
            repository_root=ROOT,
        )
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        WeChatDailyProductionError,
        WeChatRuntimeError,
    ) as exc:
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
