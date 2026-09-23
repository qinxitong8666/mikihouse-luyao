from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import subprocess
from pathlib import Path

from mikihouse_luyao.quote_assistant_app import (
    PROGRESS_PREFIX,
    build_generate_command,
    build_production_command,
    format_progress_event,
    load_dashboard_summary,
    parse_progress_line,
)


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "MIKI HOUSE 报价助手.app"


def test_progress_event_round_trip_is_machine_readable() -> None:
    line = format_progress_event(
        "FX_READY",
        30,
        "当日汇率已获取并冻结",
        rate="0.0426",
        rate_date="2026-09-23",
    )
    assert line.startswith(PROGRESS_PREFIX)
    event = parse_progress_line(line)
    assert event == {
        "event": "progress",
        "stage": "FX_READY",
        "percent": 30,
        "message": "当日汇率已获取并冻结",
        "details": {"rate": "0.0426", "rate_date": "2026-09-23"},
    }
    assert parse_progress_line("ordinary output") is None
    assert parse_progress_line(PROGRESS_PREFIX + "not json") is None


def test_dashboard_reads_latest_daily_quote_without_recalculation(tmp_path: Path) -> None:
    latest = tmp_path / "最新"
    latest.mkdir()
    pdf = latest / "MIKIHOUSE_2026-09-23_报价全集.pdf"
    pdf.write_bytes(b"x" * 1024)
    (latest / "daily_quote_stats.json").write_text(
        json.dumps(
            {
                "status": "SUCCESS",
                "quote_date": "2026-09-23",
                "included_in_stock_product_count": 1786,
                "fx": {
                    "jpy_to_cny_rate": "0.04262807348615196758616861853",
                    "rate_date": "2026-09-22",
                    "provider": "ECB_EUROFXREF_DAILY",
                },
                "pdf": {"path": pdf.name, "size_mb": 23.28},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    summary = load_dashboard_summary(latest)
    assert summary["available"] is True
    assert summary["quote_date"] == "2026-09-23"
    assert summary["product_count"] == 1786
    assert summary["pdf_size"] == "23.28 MB"
    assert summary["pdf_path"] == str(pdf)


def test_desktop_commands_only_delegate_to_existing_safe_entrypoints() -> None:
    python = Path("/tmp/venv/python")
    generate = build_generate_command(python, ROOT)
    production = build_production_command(python, ROOT, "exact-confirmation")
    assert generate == [
        str(python),
        str(ROOT / "scripts" / "generate_daily_quote.py"),
        "--progress-jsonl",
    ]
    assert production == [
        str(python),
        str(ROOT / "scripts" / "run_mikihouse_daily_production.py"),
        "--production-save",
        "--confirm",
        "exact-confirmation",
        "--progress-jsonl",
    ]


def test_tracked_app_bundle_is_native_double_clickable() -> None:
    plist_path = BUNDLE / "Contents" / "Info.plist"
    executable = BUNDLE / "Contents" / "MacOS" / "mikihouse-quote-assistant"
    assert plist_path.is_file()
    assert executable.is_file()
    assert os.access(executable, os.X_OK)
    with plist_path.open("rb") as handle:
        plist = plistlib.load(handle)
    assert plist["CFBundlePackageType"] == "APPL"
    assert plist["CFBundleDisplayName"] == "MIKI HOUSE 报价助手"
    assert plist["CFBundleExecutable"] == executable.name
    kind = subprocess.run(
        ["/usr/bin/file", str(executable)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "Mach-O" in kind
    assert subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(BUNDLE)],
        capture_output=True,
    ).returncode == 0
    requirement_result = subprocess.run(
        ["/usr/bin/codesign", "-dr", "-", str(BUNDLE)],
        check=True,
        capture_output=True,
        text=True,
    )
    requirement = requirement_result.stdout + requirement_result.stderr
    assert (
        'designated => identifier "cn.luyao.mikihouse.quoteassistant"'
        in requirement
    )
    source = (ROOT / "macos" / "MikihouseQuoteAssistant" / "main.swift").read_text()
    assert "scripts/generate_daily_quote.py" in source
    assert "scripts/run_mikihouse_daily_production.py" in source
    assert "scripts/check_quote_assistant_runtime.py" in source
    assert "scripts/create_quote_assistant_one_time_authorization.py" in source
    assert "scripts/audit_wechat_daily_favorite_titles.py" in source
    assert "scripts/audit_wechat_frozen_pdf_recovery.py" in source
    assert "--app-authorization-file" in source
    assert "--rebuild-missing-daily-favorites" in source
    assert "--recover-frozen-pdf" in source
    assert "选择仓库…" in source
    assert "安全重建已删除收藏" in source
    assert "恢复冻结PDF附件" in source
    assert "我确认修复现有1条PDF收藏并创建待处理的1条文字收藏" in source
    assert "我确认当天两条收藏均已人工删除" in source
    assert "repository_path.txt" in source
    assert 'environment.removeValue(forKey: key)' in source
    assert '"__CFBundleIdentifier", "XPC_SERVICE_NAME", "XPC_FLAGS"' in source
    assert 'environment["PWD"] = repositoryRoot.path' in source


def test_bundle_builder_reproduces_valid_structure(tmp_path: Path) -> None:
    script = ROOT / "scripts" / "build_mikihouse_quote_assistant_app.py"
    spec = importlib.util.spec_from_file_location("bundle_builder", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = tmp_path / "MIKI HOUSE 报价助手.app"
    report = module.build_bundle(output)
    assert report["status"] == "PASS"
    assert report["designated_requirement"] == (
        'designated => identifier "cn.luyao.mikihouse.quoteassistant"'
    )
    executable = output / "Contents" / "MacOS" / "mikihouse-quote-assistant"
    assert os.access(executable, os.X_OK)
    assert "Mach-O" in subprocess.run(
        ["/usr/bin/file", str(executable)], check=True, capture_output=True, text=True
    ).stdout
    assert subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(output)],
        capture_output=True,
    ).returncode == 0
    requirement_result = subprocess.run(
        ["/usr/bin/codesign", "-dr", "-", str(output)],
        check=True,
        capture_output=True,
        text=True,
    )
    requirement = requirement_result.stdout + requirement_result.stderr
    assert (
        'designated => identifier "cn.luyao.mikihouse.quoteassistant"'
        in requirement
    )
    with (output / "Contents" / "Info.plist").open("rb") as handle:
        assert plistlib.load(handle)["CFBundleIdentifier"] == "cn.luyao.mikihouse.quoteassistant"


def test_default_runtime_gate_remains_disabled_and_native_app_has_no_direct_sink() -> None:
    runtime = json.loads((ROOT / "config" / "wechat_favorite_runtime.json").read_text())
    assert runtime["production_save_enabled"] is False
    app = (ROOT / "macos" / "MikihouseQuoteAssistant" / "main.swift").read_text(encoding="utf-8")
    assert "save_daily_production_favorites" not in app
    assert "WeChatFavoriteSink" not in app
    assert "/shopapi/" not in app
    assert "production_save_enabled\"] = true" not in app.lower()
    assert "我确认本次创建恰好两条微信收藏" in app
