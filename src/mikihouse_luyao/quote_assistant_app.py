from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PROGRESS_PREFIX = "MIKIHOUSE_PROGRESS "


def build_generate_command(python: Path, repository_root: Path) -> list[str]:
    return [
        str(python),
        str(repository_root / "scripts" / "generate_daily_quote.py"),
        "--progress-jsonl",
    ]


def build_production_command(
    python: Path,
    repository_root: Path,
    confirmation: str,
) -> list[str]:
    return [
        str(python),
        str(repository_root / "scripts" / "run_mikihouse_daily_production.py"),
        "--production-save",
        "--confirm",
        confirmation,
        "--progress-jsonl",
    ]


def format_progress_event(
    stage: str,
    percent: int,
    message: str,
    **details: Any,
) -> str:
    payload = {
        "event": "progress",
        "stage": stage,
        "percent": max(0, min(100, int(percent))),
        "message": message,
        "details": details,
    }
    return PROGRESS_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def parse_progress_line(line: str) -> dict[str, Any] | None:
    if not line.startswith(PROGRESS_PREFIX):
        return None
    try:
        payload = json.loads(line[len(PROGRESS_PREFIX) :])
    except (json.JSONDecodeError, TypeError):
        return None
    if payload.get("event") != "progress":
        return None
    if not isinstance(payload.get("stage"), str) or not isinstance(payload.get("message"), str):
        return None
    percent = payload.get("percent")
    if not isinstance(percent, int) or isinstance(percent, bool) or not 0 <= percent <= 100:
        return None
    return payload


def load_dashboard_summary(latest_dir: Path) -> dict[str, Any]:
    stats_path = latest_dir / "daily_quote_stats.json"
    if not stats_path.is_file():
        return {
            "available": False,
            "quote_date": "尚未生成",
            "fx_rate": "—",
            "fx_date": "—",
            "product_count": "—",
            "pdf_size": "—",
            "pdf_path": None,
            "status": "NO_DAILY_QUOTE",
        }
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    pdf = dict(stats.get("pdf") or {})
    fx = dict(stats.get("fx") or {})
    pdf_name = str(pdf.get("path") or "")
    pdf_path = latest_dir / pdf_name if pdf_name else None
    size_mb = pdf.get("size_mb")
    if size_mb is None and pdf_path is not None and pdf_path.is_file():
        size_mb = round(pdf_path.stat().st_size / 1024 / 1024, 2)
    return {
        "available": True,
        "quote_date": str(stats.get("quote_date") or "—"),
        "fx_rate": str(fx.get("jpy_to_cny_rate") or "—"),
        "fx_date": str(fx.get("rate_date") or "—"),
        "fx_provider": str(fx.get("provider") or "—"),
        "product_count": int(stats.get("included_in_stock_product_count") or 0),
        "pdf_size": f"{float(size_mb):.2f} MB" if size_mb is not None else "—",
        "pdf_path": str(pdf_path) if pdf_path is not None else None,
        "status": str(stats.get("status") or "UNKNOWN"),
    }
