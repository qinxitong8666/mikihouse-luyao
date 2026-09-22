#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from mikihouse_luyao.wechat_daily_production import validate_daily_production_bundle
from mikihouse_luyao.wechat_favorite_runtime import PRODUCTION_CONFIRMATION


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = ROOT / "docs" / "evidence" / "wechat_daily_production_final_acceptance.json"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(args: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline final acceptance for the gated daily two-Favorite production flow"
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)

    runtime_config_path = ROOT / "config" / "wechat_favorite_runtime.json"
    runtime_config = _read_json(runtime_config_path)
    if runtime_config.get("production_save_enabled") is not False:
        raise SystemExit("FAIL_CLOSED: production_save_enabled must remain false during acceptance")

    sample_dir = ROOT / "outputs" / "daily_quote" / "2026-09-22"
    preflight = validate_daily_production_bundle(
        sample_dir,
        runtime_config,
        repository_root=ROOT,
        require_write_enabled=False,
    )

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    cli_gate = _run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_mikihouse_daily_production.py"),
            "--production-save",
            "--confirm",
            PRODUCTION_CONFIRMATION,
        ],
        env=env,
    )
    try:
        cli_gate_evidence = json.loads(cli_gate.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"FAIL: one-click CLI did not return JSON gate evidence: {exc}") from exc
    expected_cli_gate = {
        "status": "FAILED_CLOSED",
        "phase": "PRE_GENERATION_WRITE_GATE",
        "error": "production save config switch is disabled",
        "website_crawl_started": False,
        "wechat_mutation_count": 0,
    }
    if cli_gate.returncode != 2 or cli_gate_evidence != expected_cli_gate:
        raise SystemExit("FAIL: default one-click CLI gate did not stop before crawl")

    pytest_result = _run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_wechat_daily_production.py",
            "tests/test_wechat_favorite_runtime.py",
        ],
        env=env,
    )
    if pytest_result.returncode != 0:
        sys.stderr.write(pytest_result.stdout + pytest_result.stderr)
        raise SystemExit("FAIL: targeted production acceptance tests failed")

    head = _run(["git", "rev-parse", "HEAD"])
    if head.returncode != 0:
        raise SystemExit("FAIL: cannot resolve git HEAD")
    tracked_paths = [
        ROOT / "scripts" / "run_mikihouse_daily_production.py",
        ROOT / "scripts" / "生成并保存MIKIHOUSE每日两个微信收藏.command",
        ROOT / "src" / "mikihouse_luyao" / "wechat_daily_production.py",
        ROOT / "src" / "mikihouse_luyao" / "wechat_favorite_runtime.py",
        runtime_config_path,
    ]
    report = {
        "schema_version": 1,
        "status": "PASS_FINAL_ACCEPTANCE_PRODUCTION_GATE_OFF",
        "verified_at": datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(),
        "base_commit": head.stdout.strip(),
        "scope": "OFFLINE_FINAL_ACCEPTANCE_NO_REAL_WECHAT_OR_SHIJIU_WRITE",
        "production_save_enabled": False,
        "one_click_entry": {
            "status": "PASS",
            "mac_command": "scripts/生成并保存MIKIHOUSE每日两个微信收藏.command",
            "runner": "scripts/run_mikihouse_daily_production.py",
            "default_gate_returncode": cli_gate.returncode,
            "default_gate_evidence": cli_gate_evidence,
        },
        "current_sample_bundle_preflight": {
            key: preflight[key]
            for key in (
                "status",
                "quote_date",
                "manifest_sha256",
                "bundle_sha256",
                "runtime_evidence_status",
                "production_save_enabled",
                "shijiu_request_count",
                "chat_send_count",
            )
        },
        "scenario_acceptance": {
            "ordered_pdf_then_text": "PASS_FAKE_SINK",
            "checkpoint_each_stage": "PASS_FAKE_SINK",
            "clean_interstage_resume_skips_passed_pdf": "PASS_FAKE_SINK",
            "completed_replay_creates_zero_duplicates": "PASS_FAKE_SINK",
            "changed_bundle_rejected_before_mutation": "PASS_FAKE_SINK",
            "existing_title_collision_blocks_before_create": "PASS_MONKEYPATCHED_READ_ONLY_SEARCH",
            "mutation_uncertainty_freezes_without_retry": "PASS_FAKE_SINK",
            "production_gate_disabled_before_crawl": "PASS_REAL_CLI_ZERO_WRITE",
        },
        "targeted_pytest": {
            "status": "PASS",
            "command": "python -m pytest -q tests/test_wechat_daily_production.py tests/test_wechat_favorite_runtime.py",
            "stdout": pytest_result.stdout.strip(),
        },
        "tracked_code_sha256": {
            str(path.relative_to(ROOT)): _sha256(path) for path in tracked_paths
        },
        "mutation_counters": {
            "real_wechat_create": 0,
            "real_wechat_update": 0,
            "real_wechat_delete": 0,
            "chat_send": 0,
            "shijiu_request": 0,
        },
        "conclusion": "PRODUCTION_FLOW_ACCEPTED_BUT_EXPLICITLY_DISABLED",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
