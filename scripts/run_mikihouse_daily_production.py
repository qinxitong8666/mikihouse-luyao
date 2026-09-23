#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mikihouse_luyao.daily_quote import DailyQuoteError
from mikihouse_luyao.daily_quote_fx import FxError
from mikihouse_luyao.daily_quote_guard import CHECKPOINT_FILE
from mikihouse_luyao.daily_quote_runner import DailyQuoteRunError, run_daily_quote
from mikihouse_luyao.quote_assistant_app import format_progress_event
from mikihouse_luyao.quote_assistant_authorization import (
    OPERATION_CREATE_DAILY,
    OPERATION_RECOVER_FROZEN_PDF,
    OPERATION_REBUILD_MISSING_DAILY,
    QuoteAssistantAuthorizationError,
    validate_and_consume_one_time_authorization,
)
from mikihouse_luyao.scraper import ScrapeError
from mikihouse_luyao.wechat_daily_production import (
    WeChatDailyProductionError,
    recover_frozen_pdf_and_complete_daily_favorites,
    save_daily_production_favorites,
)
from mikihouse_luyao.wechat_favorite_runtime import PRODUCTION_CONFIRMATION, WeChatRuntimeError


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
    parser.add_argument(
        "--app-authorization-file",
        type=Path,
        help="private, short-lived permit created by MIKI HOUSE 报价助手.app",
    )
    parser.add_argument(
        "--rebuild-missing-daily-favorites",
        action="store_true",
        help="rebuild a PASS/frozen checkpoint from the current bundle only after App authorization and read-only proof that both titles are absent",
    )
    parser.add_argument(
        "--recover-frozen-pdf",
        action="store_true",
        help="recover the one frozen text-only PDF note through the toolbar picker, then create the untouched text note",
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
    production_authorization: dict[str, Any] | None = None
    emit("PRODUCTION_GATE", 1, "正在检查微信正式保存门禁")
    if args.rebuild_missing_daily_favorites and args.recover_frozen_pdf:
        print(json.dumps({
            "status": "FAILED_CLOSED",
            "phase": "PRE_GENERATION_WRITE_GATE",
            "error": "安全重建与PDF冻结恢复不能同时执行。",
            "website_crawl_started": False,
            "wechat_mutation_count": 0,
        }, ensure_ascii=False, indent=2))
        return 2
    if (args.rebuild_missing_daily_favorites or args.recover_frozen_pdf) and not args.production_save:
        print(json.dumps({
            "status": "FAILED_CLOSED",
            "phase": "PRE_GENERATION_WRITE_GATE",
            "error": "安全重建必须使用 App 正式生产模式。",
            "website_crawl_started": False,
            "wechat_mutation_count": 0,
        }, ensure_ascii=False, indent=2))
        return 2
    if args.production_save:
        if args.confirm != PRODUCTION_CONFIRMATION:
            print(json.dumps({
                "status": "FAILED_CLOSED",
                "phase": "PRE_GENERATION_WRITE_GATE",
                "error": "确认内容不匹配，未开始官网抓取或微信写入。",
                "website_crawl_started": False,
                "wechat_mutation_count": 0,
            }, ensure_ascii=False, indent=2))
            return 2
        if args.app_authorization_file is not None:
            try:
                expected_operation = (
                    OPERATION_RECOVER_FROZEN_PDF
                    if args.recover_frozen_pdf
                    else (
                        OPERATION_REBUILD_MISSING_DAILY
                        if args.rebuild_missing_daily_favorites
                        else OPERATION_CREATE_DAILY
                    )
                )
                production_authorization = validate_and_consume_one_time_authorization(
                    args.app_authorization_file,
                    ROOT,
                    confirmation=args.confirm,
                    expected_operation=expected_operation,
                )
            except (QuoteAssistantAuthorizationError, OSError, ValueError) as exc:
                print(json.dumps({
                    "status": "FAILED_CLOSED",
                    "phase": "PRE_GENERATION_WRITE_GATE",
                    "error": str(exc),
                    "website_crawl_started": False,
                    "wechat_mutation_count": 0,
                }, ensure_ascii=False, indent=2))
                return 2
            runtime_config = {
                **runtime_config,
                "production_save_enabled": True,
                "production_authorization_mode": "APP_ONE_TIME",
                "production_authorization_operation": expected_operation,
            }
        elif runtime_config.get("production_save_enabled") is True:
            if args.rebuild_missing_daily_favorites or args.recover_frozen_pdf:
                print(json.dumps({
                    "status": "FAILED_CLOSED",
                    "phase": "PRE_GENERATION_WRITE_GATE",
                    "error": "安全重建只允许从报价助手 App 发起并消费专用单次授权。",
                    "website_crawl_started": False,
                    "wechat_mutation_count": 0,
                }, ensure_ascii=False, indent=2))
                return 2
            production_authorization = {
                "status": "ACCEPTED",
                "authorization_mode": "TRACKED_CONFIG_AND_EXACT_CONFIRMATION",
                "reusable": False,
            }
        else:
            print(json.dumps({
                "status": "FAILED_CLOSED",
                "phase": "PRE_GENERATION_WRITE_GATE",
                "error": "仓库默认生产开关关闭；请从 MIKI HOUSE 报价助手.app 完成本次单次授权。",
                "website_crawl_started": False,
                "wechat_mutation_count": 0,
            }, ensure_ascii=False, indent=2))
            return 2
    try:
        if args.recover_frozen_pdf:
            quote_date = args.quote_date or datetime.now(
                ZoneInfo("Asia/Tokyo")
            ).date().isoformat()
            daily_dir = args.output_root / quote_date
            emit("PDF_RECOVERY_PREFLIGHT", 10, "正在核对冻结checkpoint与精确收藏标题")
            recovered = recover_frozen_pdf_and_complete_daily_favorites(
                daily_dir,
                runtime_config,
                repository_root=ROOT,
                confirmation=args.confirm,
            )
            emit("COMPLETE", 100, "PDF附件已恢复，文字收藏已保存并通过强回读")
            print(json.dumps({
                "status": "SUCCESS",
                "production_authorization": production_authorization,
                "daily_quote": {
                    "status": "REUSED_FROZEN_DAILY_BUNDLE",
                    "output_dir": str(daily_dir.resolve()),
                    "website_crawl_started": False,
                },
                "wechat_two_favorites": recovered,
            }, ensure_ascii=False, indent=2))
            return 0

        def quote_progress(
            stage: str,
            percent: int,
            message: str,
            details: dict[str, Any],
        ) -> None:
            emit(stage, 4 + int(percent * 0.72), message, **details)

        quote_date = args.quote_date or datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()
        daily_dir = args.output_root / quote_date
        has_checkpoint = (daily_dir / CHECKPOINT_FILE).exists() or (daily_dir / CHECKPOINT_FILE).is_symlink()
        if args.rebuild_missing_daily_favorites and not has_checkpoint:
            raise WeChatDailyProductionError("安全重建需要当天已有 PASS 或冻结 checkpoint，不会先重新生成报价")
        if args.production_save and has_checkpoint:
            if args.fx_rate or args.source_snapshot:
                raise WeChatDailyProductionError("已有 checkpoint，禁止使用汇率或快照覆盖参数重新生成当天 bundle")
            emit("REUSE_CHECKPOINT_BUNDLE", 78, "当天已有checkpoint：复用当前bundle，禁止先覆盖报价文件")
            generated = {
                "status": "REUSED_CHECKPOINT_PROTECTED_BUNDLE",
                "output_dir": str(daily_dir.resolve()),
                "website_crawl_started": False,
            }
        else:
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
            rebuild_missing_daily=args.rebuild_missing_daily_favorites,
        )
        emit("COMPLETE", 100, "两条微信收藏已保存并通过强回读")
    except (
        DailyQuoteError,
        DailyQuoteRunError,
        FxError,
        ScrapeError,
        WeChatDailyProductionError,
        WeChatRuntimeError,
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
        "production_authorization": production_authorization,
        "daily_quote": generated,
        "wechat_two_favorites": saved,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
