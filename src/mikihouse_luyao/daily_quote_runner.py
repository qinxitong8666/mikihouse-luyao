from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .catalog import fetch_all_storefront_products
from .daily_quote import (
    ALLOWED_CATEGORIES,
    CATEGORY_LABELS,
    DAILY_QUOTE_ELIGIBLE,
    DailyQuoteError,
    build_daily_quote_manifest,
    compare_manifests,
    load_special_numbers,
    sha256_json,
)
from .daily_quote_fx import FrozenFxRate, FxError, fetch_ecb_reference_rate, manual_fx_rate
from .daily_quote_guard import locked_daily_output, require_unprotected_quote_directory
from .daily_quote_images import prepare_product_thumbnails
from .daily_quote_pdf import generate_daily_quote_pdf, validate_daily_quote_pdf
from .daily_quote_text import (
    build_favorite_payloads,
    render_favorite_preview,
    render_production_compact_text_quote,
    render_text_quote,
    text_stats,
    write_json,
)
from .scraper import ScrapeError
from .quote_assistant_app import format_progress_event


class DailyQuoteRunError(RuntimeError):
    pass


ProgressCallback = Callable[[str, int, str, dict[str, Any]], None]


def _progress(
    callback: ProgressCallback | None,
    stage: str,
    percent: int,
    message: str,
    **details: Any,
) -> None:
    if callback is not None:
        callback(stage, percent, message, details)


def _frozen_fx_progress_details(fx: FrozenFxRate) -> dict[str, str]:
    """Return display-only progress values without treating the dataclass as a mapping."""

    return {
        "rate": format(fx.jpy_to_cny_rate, "f"),
        "rate_date": fx.rate_date,
        "provider": fx.provider,
    }


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _gzip_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    with path.open("wb") as raw_handle:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_handle, compresslevel=9, mtime=0) as handle:
            handle.write(data)


def _load_source_snapshot(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
    else:
        payload = _read_json(path)
    products = list(payload.get("products") or [])
    if not payload.get("complete_pagination_validated"):
        raise DailyQuoteRunError("offline source snapshot is not marked complete")
    return products, dict(payload.get("crawl") or {"storefront_product_count": len(products), "offline_snapshot": True})


def _validate_crawl(
    products: list[dict[str, Any]], crawl: dict[str, Any], config: dict[str, Any], previous: dict[str, Any] | None
) -> None:
    count = len(products)
    if count != int(crawl.get("storefront_product_count", count)):
        raise DailyQuoteRunError("crawl count does not match unique normalized products")
    if count < int(config["crawl_minimum_product_count"]):
        raise DailyQuoteRunError(f"crawl product count is abnormally low: {count}")
    if len({row.get("product_number") for row in products}) != count:
        raise DailyQuoteRunError("crawl contains duplicate product numbers")
    if previous:
        previous_count = int(previous.get("source_snapshot_product_count") or 0)
        if previous_count:
            drop = Decimal(previous_count - count) / Decimal(previous_count)
            if drop > Decimal(str(config["crawl_max_drop_ratio"])):
                raise DailyQuoteRunError(f"crawl product count dropped by {drop:.2%}")


def _internal_report(manifest: dict[str, Any], diff: dict[str, Any], stats: dict[str, Any]) -> str:
    counts = manifest["counts"]
    excluded = counts["excluded_reason_counts"]
    lines = [
        f"MIKI HOUSE {manifest['quote_date']} 内部变化报告",
        "",
        f"官网总商品数：{counts['storefront_product_count']}",
        f"白名单分类商品数：{counts['whitelist_category_product_count']}",
        f"最终eligible数量：{counts['eligible_product_count']}",
        f"实际有货并进入报价数量：{counts['included_in_stock_product_count']}",
        f"NO_DISCOUNT_LIST排除数：{excluded.get('NO_DISCOUNT_LIST', 0)}",
        f"WEB_EXCLUSIVE排除数：{excluded.get('WEB_EXCLUSIVE', 0)}",
        f"LIMITED_TIME_PRICE排除数：{excluded.get('LIMITED_TIME_PRICE', 0)}",
        f"NON_SELLABLE_SERVICE_OR_ADDON排除数：{excluded.get('NON_SELLABLE_SERVICE_OR_ADDON', 0)}",
        f"非白名单分类排除数：{excluded.get('NON_WHITELIST_CATEGORY', 0)}",
        f"抓取/资源异常数：{len(stats.get('failures') or [])}",
        f"FX：{manifest['fx']['jpy_to_cny_rate']}（{manifest['fx']['rate_date']}，{manifest['fx']['provider']}）",
        "",
        f"今日新增商品：{len(diff['new_products'])}",
        *[f"  + {value}" for value in diff["new_products"]],
        f"今日退出报价商品：{len(diff['exited_products'])}",
        *[f"  - {value}" for value in diff["exited_products"]],
        f"官网JPY价格变化：{len(diff['source_jpy_price_changes'])}",
        f"最终CNY报价变化：{len(diff['customer_cny_price_changes'])}",
        f"新售罄SKU：{len(diff['newly_sold_out_variants'])}",
        f"恢复有货SKU：{len(diff['restored_available_variants'])}",
        f"新增variant：{len(diff['new_variants'])}",
        "",
        "客户输出安全：不包含官网JPY原价、折扣率、汇率或定价公式。",
        "Shijiu请求/写入：0。微信收藏真实写入：0。",
    ]
    return "\n".join(lines).rstrip() + "\n"


def _replace_latest(output_root: Path, quote_date: str) -> None:
    latest = output_root / "最新"
    temporary = output_root / ".latest-next"
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(quote_date, target_is_directory=True)
    if latest.exists() and not latest.is_symlink():
        raise DailyQuoteRunError("outputs/daily_quote/最新 exists but is not a symlink")
    temporary.replace(latest)


@locked_daily_output
def run_daily_quote(
    *,
    config_path: Path,
    special_path: Path,
    output_root: Path,
    cache_dir: Path,
    quote_date: str | None = None,
    manual_rate: str | None = None,
    source_snapshot_path: Path | None = None,
    page_size: int = 100,
    delay: float = 0.1,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    _progress(progress_callback, "INITIALIZING", 2, "正在检查配置和安全门禁")
    config = _read_json(config_path)
    if config.get("shijiu_requests_enabled") is not False or config.get("wechat_write_enabled") is not False:
        raise DailyQuoteRunError("daily quote config must keep Shijiu and WeChat writes disabled")
    today = date.fromisoformat(quote_date) if quote_date else datetime.now(ZoneInfo("Asia/Tokyo")).date()
    quote_date_text = today.isoformat()
    require_unprotected_quote_directory(output_root / quote_date_text)
    output_root.mkdir(parents=True, exist_ok=True)
    previous_path = output_root / "last_successful_manifest.json"
    previous = _read_json(previous_path) if previous_path.exists() else None
    special = load_special_numbers(special_path)
    _progress(progress_callback, "SOURCE_CRAWL", 8, "正在完整抓取 MIKI HOUSE 官网")
    if source_snapshot_path:
        products, crawl = _load_source_snapshot(source_snapshot_path)
    else:
        products, crawl = fetch_all_storefront_products(
            set(), page_size=page_size, delay=delay, timeout=30, retries=2, max_pages=1000
        )
    crawl = {**crawl, "complete_pagination_validated": True}
    _validate_crawl(products, crawl, config, previous)
    _progress(
        progress_callback,
        "SOURCE_READY",
        24,
        "官网全量商品抓取与分页校验完成",
        product_count=len(products),
    )
    fx = manual_fx_rate(manual_rate, quote_date=today) if manual_rate else fetch_ecb_reference_rate(
        quote_date=today, max_staleness_days=int(config["fx_max_staleness_days"])
    )
    _progress(
        progress_callback,
        "FX_READY",
        30,
        "当日汇率已获取并冻结",
        **_frozen_fx_progress_details(fx),
    )
    generated_at = datetime.now(ZoneInfo("Asia/Tokyo")).isoformat()
    preliminary = build_daily_quote_manifest(
        products,
        special_numbers=special,
        fx=fx,
        discount_rate=Decimal(str(config["discount_rate"])),
        quote_date=quote_date_text,
        generated_at=generated_at,
        crawl_stats=crawl,
    )
    eligible_numbers = {row["product_number"] for row in preliminary["products"]}
    source_eligible = [row for row in products if row["product_number"] in eligible_numbers]
    _progress(
        progress_callback,
        "ELIGIBILITY_READY",
        38,
        "客户可报价商品池已生成",
        eligible_product_count=len(source_eligible),
    )
    assets, image_failures = prepare_product_thumbnails(
        source_eligible,
        cache_dir=cache_dir,
        long_edge_px=int(config["thumbnail_long_edge_px"]),
        jpeg_quality=int(config["thumbnail_jpeg_quality"]),
    )
    failure_map = {row["product_number"]: row["error"] for row in image_failures}
    failure_ratio = Decimal(len(image_failures)) / Decimal(max(1, len(source_eligible)))
    if failure_ratio > Decimal("0.02"):
        raise DailyQuoteRunError(f"image failure ratio exceeds 2%: {failure_ratio:.2%}")
    _progress(
        progress_callback,
        "THUMBNAILS_READY",
        58,
        "客户 PDF 缩略图已完成",
        thumbnail_count=len(assets),
        failure_count=len(image_failures),
    )
    manifest = build_daily_quote_manifest(
        products,
        special_numbers=special,
        fx=fx,
        discount_rate=Decimal(str(config["discount_rate"])),
        quote_date=quote_date_text,
        generated_at=generated_at,
        main_image_hashes={number: row["content_sha256"] for number, row in assets.items()},
        main_image_assets=assets,
        media_failures=failure_map,
        crawl_stats=crawl,
    )
    included_numbers = {row["product_number"] for row in manifest["products"]}
    thumbnail_paths = {
        number: Path(row["thumbnail_path"])
        for number, row in assets.items()
        if number in included_numbers
    }
    diff = compare_manifests(previous, manifest)
    build_parent = output_root / ".build"
    build_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{quote_date_text}-", dir=build_parent) as temp_name:
        work = Path(temp_name)
        full_pdf = work / f"MIKIHOUSE_{quote_date_text}_报价全集.pdf"
        _progress(progress_callback, "PDF_BUILD", 63, "正在生成可搜索客户 PDF")
        watermark_kwargs = {
            "watermark_text": str(config["customer_pdf_watermark_text"]),
            "watermark_opacity": float(config["customer_pdf_watermark_opacity"]),
        }
        pdf_report = generate_daily_quote_pdf(
            manifest, thumbnail_paths, full_pdf, **watermark_kwargs
        )
        pdf_validation = validate_daily_quote_pdf(full_pdf, manifest, pdf_report, sample_size=50)
        pdf_report["path"] = full_pdf.name
        _progress(
            progress_callback,
            "PDF_READY",
            82,
            "PDF 已生成并通过自动检索验收",
            page_count=pdf_report["page_count"],
            product_count=pdf_report["product_count"],
            size_mb=round(full_pdf.stat().st_size / 1024 / 1024, 3),
        )
        threshold = int(config["mobile_share_pdf_max_mb"]) * 1024 * 1024
        category_pdfs: dict[str, Path] | None = None
        category_reports: dict[str, Any] = {}
        if full_pdf.stat().st_size > threshold:
            category_pdfs = {}
            for category in ALLOWED_CATEGORIES:
                path = work / f"MIKIHOUSE_{quote_date_text}_{CATEGORY_LABELS[category]}.pdf"
                report = generate_daily_quote_pdf(
                    manifest,
                    thumbnail_paths,
                    path,
                    categories=(category,),
                    **watermark_kwargs,
                )
                validation = validate_daily_quote_pdf(path, manifest, report, sample_size=50)
                report["path"] = path.name
                category_pdfs[category] = path
                category_reports[category] = {**report, "validation": validation}
        text_quote = render_text_quote(
            manifest, include_product_name=bool(config["include_product_name_in_text"])
        )
        text_path = work / f"MIKIHOUSE_{quote_date_text}_文字报价.txt"
        _write_text(text_path, text_quote)
        text_report = text_stats(text_quote, len(manifest["products"]))
        favorite_text_format = str(config.get("wechat_text_format") or "VERBOSE").upper()
        if favorite_text_format == "LOSSLESS_COMPACT":
            favorite_text_quote = render_production_compact_text_quote(manifest)
        elif favorite_text_format == "VERBOSE":
            favorite_text_quote = text_quote
        else:
            raise DailyQuoteRunError(f"unsupported wechat_text_format: {favorite_text_format}")
        pdf_payload, text_payload = build_favorite_payloads(
            manifest,
            text_quote=favorite_text_quote,
            full_pdf=full_pdf,
            category_pdfs=category_pdfs,
            max_pdf_mb=int(config["mobile_share_pdf_max_mb"]),
        )
        text_payload["text_format"] = favorite_text_format
        text_payload["capacity_readiness"] = config.get("wechat_text_capacity_status")
        # Persist relative attachment names so the preview is portable after the atomic directory move.
        pdf_payload["attachments"] = [Path(value).name for value in pdf_payload["attachments"]]
        _write_text(work / "wechat_pdf_favorite_preview.txt", render_favorite_preview(pdf_payload))
        write_json(work / "wechat_pdf_favorite_payload.json", pdf_payload)
        _write_text(work / "wechat_text_favorite_preview.txt", render_favorite_preview(text_payload))
        write_json(work / "wechat_text_favorite_payload.json", text_payload)
        write_json(work / "daily_quote_manifest.json", manifest)
        source_snapshot = {
            "schema_version": 1,
            "catalog_kind": "MIKIHOUSE_DAILY_QUOTE_COMPLETE_SOURCE_SNAPSHOT",
            "captured_at": crawl.get("synced_at") or generated_at,
            "complete_pagination_validated": True,
            "source_snapshot_sha256": manifest["source_snapshot_sha256"],
            "crawl": crawl,
            "products": products,
        }
        _gzip_json(work / "source_snapshot.json.gz", source_snapshot)
        thumb_stats = {
            "count": len(thumbnail_paths),
            "total_bytes": sum(assets[number]["thumbnail_byte_count"] for number in thumbnail_paths),
            "deduplicated_content_count": len({assets[number]["thumbnail_sha256"] for number in thumbnail_paths}),
            "long_edge_px": config["thumbnail_long_edge_px"],
            "jpeg_quality": config["thumbnail_jpeg_quality"],
            "items": [
                {
                    "product_number": number,
                    "sha256": assets[number]["thumbnail_sha256"],
                    "byte_count": assets[number]["thumbnail_byte_count"],
                    "width": assets[number]["thumbnail_width"],
                    "height": assets[number]["thumbnail_height"],
                }
                for number in sorted(thumbnail_paths)
            ],
        }
        qa_report = {
            "status": "AUTOMATED_PASS_VISUAL_QA_PENDING",
            "pdf": {**pdf_report, "validation": pdf_validation},
            "category_pdfs": category_reports,
            "thumbnail_compression": thumb_stats,
            "mobile_share_pdf_max_mb": config["mobile_share_pdf_max_mb"],
            "full_pdf_over_limit": full_pdf.stat().st_size > threshold,
            "visual_qa": {"required_dpi": 200, "status": "PENDING_MANUAL_REVIEW"},
        }
        write_json(work / "PDF压缩检索验收报告.json", qa_report)
        failures = {
            "status": "PASS_WITH_EXCLUSIONS" if image_failures else "PASS",
            "crawl_failures": [],
            "resource_failures": image_failures,
            "failure_count": len(image_failures),
        }
        write_json(work / "failures.json", failures)
        stats = {
            "schema_version": 1,
            "status": "SUCCESS_VISUAL_QA_PENDING",
            "quote_date": quote_date_text,
            "manifest_sha256": manifest["manifest_sha256"],
            "source_snapshot_sha256": manifest["source_snapshot_sha256"],
            "storefront_product_count": manifest["counts"]["storefront_product_count"],
            "whitelist_category_counts": manifest["counts"]["whitelist_category_counts"],
            "eligible_product_count": manifest["counts"]["eligible_product_count"],
            "included_in_stock_product_count": manifest["counts"]["included_in_stock_product_count"],
            "excluded_reason_counts": manifest["counts"]["excluded_reason_counts"],
            "fx": manifest["fx"],
            "pdf": {**pdf_report, "size_mb": round(full_pdf.stat().st_size / 1024 / 1024, 3)},
            "category_pdfs": category_reports,
            "text_quote": text_report,
            "favorite_preview_count": 2,
            "wechat_real_write_count": 0,
            "shijiu_request_count": 0,
            "shijiu_mutation_count": 0,
            "writer_mutex_evidence_count": 0,
            "failures": image_failures,
            "diff": diff,
        }
        write_json(work / "daily_quote_stats.json", stats)
        _write_text(work / f"MIKIHOUSE_{quote_date_text}_内部变化报告.txt", _internal_report(manifest, diff, stats))
        _progress(progress_callback, "FINALIZING", 94, "正在原子发布当日输出并更新最新目录")
        final_dir = output_root / quote_date_text
        require_unprotected_quote_directory(final_dir)
        final_dir.mkdir(parents=True, exist_ok=True)
        for child in work.iterdir():
            target = final_dir / child.name
            child.replace(target)
        _replace_latest(output_root, quote_date_text)
        previous_tmp = output_root / ".last_successful_manifest.tmp"
        previous_tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        previous_tmp.replace(previous_path)
    _progress(
        progress_callback,
        "COMPLETE",
        100,
        "今日报价生成完成",
        output_dir=str(output_root / quote_date_text),
    )
    return {
        "status": "SUCCESS_VISUAL_QA_PENDING",
        "output_dir": str(output_root / quote_date_text),
        "manifest_sha256": manifest["manifest_sha256"],
        "stats": stats,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate the MIKI HOUSE daily customer quote bundle")
    parser.add_argument("--config", type=Path, default=Path("config/daily_quote.json"))
    parser.add_argument("--special", type=Path, default=Path("special_skus_2026aw.csv"))
    parser.add_argument("--output-root", type=Path, default=Path("outputs/daily_quote"))
    parser.add_argument("--thumbnail-cache", type=Path, default=Path("output/daily-quote-thumbnail-cache"))
    parser.add_argument("--quote-date", help="Asia/Tokyo quote date, YYYY-MM-DD")
    parser.add_argument("--fx-rate", help="explicit JPY-to-CNY Decimal override; recorded as MANUAL_OVERRIDE")
    parser.add_argument("--source-snapshot", type=Path, help="complete offline source snapshot for tests/emergency runs")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--delay", type=float, default=0.1)
    parser.add_argument(
        "--progress-jsonl",
        action="store_true",
        help="emit machine-readable progress events to stderr",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    progress_callback = None
    if args.progress_jsonl:
        def progress_callback(stage: str, percent: int, message: str, details: dict[str, Any]) -> None:
            print(format_progress_event(stage, percent, message, **details), file=sys.stderr, flush=True)
    try:
        result = run_daily_quote(
            config_path=args.config,
            special_path=args.special,
            output_root=args.output_root,
            cache_dir=args.thumbnail_cache,
            quote_date=args.quote_date,
            manual_rate=args.fx_rate,
            source_snapshot_path=args.source_snapshot,
            page_size=args.page_size,
            delay=args.delay,
            progress_callback=progress_callback,
        )
    except (DailyQuoteError, DailyQuoteRunError, FxError, ScrapeError, OSError, ValueError) as exc:
        print(json.dumps({"status": "FAILED_CLOSED", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
