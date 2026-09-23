#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from mikihouse_luyao.daily_quote import DailyQuoteError
from mikihouse_luyao.daily_quote_fx import FxError
from mikihouse_luyao.daily_quote_runner import DailyQuoteRunError, run_daily_quote
from mikihouse_luyao.quote_assistant_app import format_progress_event
from mikihouse_luyao.scraper import ScrapeError
from mikihouse_luyao.wechat_daily_production import (
    WeChatDailyProductionError,
    save_daily_production_favorites,
)
from mikihouse_luyao.wechat_favorite_runtime import PRODUCTION_CONFIRMATION


ROOT = Path(__file__).resolve().parents[1]


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate one fresh MIKI HOUSE daily quote bundle and optionally save exactly two WeChat Favorites"
    )
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "daily_quote.json")
    parser.add_argument("--special", type=Path, default=ROOT / "special_skus_2026aw.csv")
    parser.add_argument("--output-root", type=Path, default=ROOT / "outputs" / "daily_quote")
    parser.add_argument(
        "--thumbnail-cache",
        type=Path,
        default=ROOT / "output" / "daily-quote-thumbnail-cache",
    )
    parser.add_argument(
        "--runtime-config",
        type=Path,
        default=ROOT / "config" / "wechat_favorite_runtime.json",
    )
    parser.add_argument("--quote-date", help="Asia/Tokyo quote date, YYYY-MM-DD")
    parser.add_argument("--fx-rate", help="explicit JPY-to-CNY Decimal override")
    parser.add_argument("--source-snapshot", type=Path)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--delay", type=float, default=0.1)
    parser.add_argument(
        "--production-save",
        action="store_true",
        help="after a fresh successful run, save exactly the PDF and LOSSLESS_COMPACT Favorites",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help="exact production confirmation; ignored in preview-only mode",
    )
    parser.add_argument(
        "--progress-jsonl",
        action="store_true",
        help="emit machine-readable progress events to stderr",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    def emit(stage: str, percent: int, message: str, **details: Any) -> None:
        if args.progress_jsonl:
            print(
                format_progress_event(stage, percent, message, **details),
                file=sys.stderr,
                flush=True,
            )

    runtime_config = _read_json(args.runtime_config)
    emit("PRODUCTION_GATE", 1, "正在检查微信正式保存门禁")
    if args.production_save:
        if runtime_config.get("production_save_enabled") is not True:
            print(json.dumps({
                "status": "FAILED_CLOSED",
                "phase": "PRE_GENERATION_WRITE_GATE",
                "error": "production save config switch is disabled",
                "website_crawl_started": False,
                "wechat_mutation_count": 0,
            }, ensure_ascii=False, indent=2))
            return 2
        if args.confirm != PRODUCTION_CONFIRMATION:
            print(json.dumps({
                "status": "FAILED_CLOSED",
                "phase": "PRE_GENERATION_WRITE_GATE",
                "error": "exact production confirmation is missing",
                "website_crawl_started": False,
                "wechat_mutation_count": 0,
            }, ensure_ascii=False, indent=2))
            return 2
    try:
        def quote_progress(
            stage: str,
            percent: int,
            message: str,
            details: dict[str, Any],
        ) -> None:
            emit(stage, 4 + int(percent * 0.72), message, **details)

        generated = run_daily_quote(
            config_path=args.config,
            special_path=args.special,
            output_root=args.output_root,
            cache_dir=args.thumbnail_cache,
            quote_date=args.quote_date,
            manual_rate=args.fx_rate,
            source_snapshot_path=args.source_snapshot,
            page_size=args.page_size,
            delay=args.delay,
            progress_callback=quote_progress,
        )
        if not args.production_save:
            print(json.dumps({
                **generated,
                "production_mode": "PREVIEW_ONLY",
                "wechat_mutation_count": 0,
                "next_gate": "explicit production authorization required",
            }, ensure_ascii=False, indent=2))
            return 0
        emit("WECHAT_PREFLIGHT", 78, "报价生成完成，正在校验双收藏正式保存前置条件")
        emit("WECHAT_SAVE", 82, "正在按 PDF 版、文字版顺序保存并强回读")
        saved = save_daily_production_favorites(
            Path(generated["output_dir"]),
            runtime_config,
            repository_root=ROOT,
            confirmation=args.confirm,
        )
        emit("COMPLETE", 100, "两条微信收藏已保存并通过强回读")
    except (
        DailyQuoteError,
        DailyQuoteRunError,
        FxError,
        ScrapeError,
        WeChatDailyProductionError,
        OSError,
        ValueError,
    ) as exc:
        print(json.dumps({
            "status": "FAILED_CLOSED",
            "error": str(exc),
            "automatic_mutation_retry_count": 0,
        }, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({
        "status": "SUCCESS",
        "daily_quote": generated,
        "wechat_two_favorites": saved,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
