#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from mikihouse_luyao.wechat_favorite_runtime import (
    MacWeChatFavoriteSink,
    PRODUCTION_CONFIRMATION,
    WeChatRuntimeError,
    write_json,
)


ROOT = Path(__file__).resolve().parents[1]


def _load_payload(daily_dir: Path, kind: str) -> dict:
    path = daily_dir / f"wechat_{kind}_favorite_payload.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    attachments = []
    for value in payload.get("attachments") or []:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = daily_dir / candidate
        attachments.append(str(candidate.resolve()))
    payload["attachments"] = attachments
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Explicitly gated Mac WeChat Favorites runtime sink"
    )
    parser.add_argument("--daily-dir", type=Path, required=True)
    parser.add_argument("--kind", choices=("pdf", "text"), required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--runtime-test", action="store_true")
    mode.add_argument("--production-save", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "wechat_favorite_runtime.json",
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    daily_dir = args.daily_dir.resolve()
    payload = _load_payload(daily_dir, args.kind)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    production = bool(args.production_save)
    if not production:
        payload["title"] = (
            f"MIKIHOUSE_TEST_{payload['quote_date']}_{args.kind.upper()}｜{payload['title']}"
        )
        payload["mode"] = "RUNTIME_TEST"
        payload["real_wechat_write_enabled"] = True
    sink = MacWeChatFavoriteSink(config)
    try:
        evidence = sink.save(
            payload,
            production=production,
            confirmation=args.confirm or None,
        )
    except WeChatRuntimeError as exc:
        print(f"BLOCKED: {exc}")
        return 2
    report_path = args.report or (
        daily_dir / f"wechat_{args.kind}_favorite_runtime_validation.json"
    )
    write_json(report_path, evidence)
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
