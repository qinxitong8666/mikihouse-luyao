from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mikihouse_luyao.daily_quote import sha256_json
from mikihouse_luyao.wechat_daily_production import (
    CHECKPOINT_FILENAME,
    PDF_FILE_PICKER_RECOVERY_MODE,
    REPORT_FILENAME,
    WeChatDailyProductionError,
    _new_checkpoint,
    save_daily_production_favorites,
    validate_daily_production_bundle,
    validate_pdf_file_picker_recovery_evidence,
)
from mikihouse_luyao.wechat_favorite_runtime import PRODUCTION_CONFIRMATION


ROOT = Path(__file__).resolve().parents[1]


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_bundle(tmp_path: Path) -> tuple[Path, dict]:
    daily = tmp_path / "2026-09-23"
    daily.mkdir()
    manifest = {
        "schema_version": 1,
        "quote_date": "2026-09-23",
        "counts": {"included_in_stock_product_count": 1},
        "products": [{"product_number": "10-0001-001"}],
    }
    manifest["manifest_sha256"] = sha256_json(manifest)
    write_json(daily / "daily_quote_manifest.json", manifest)
    pdf_name = "MIKIHOUSE_2026-09-23_报价全集.pdf"
    (daily / pdf_name).write_bytes(b"%PDF-fake-test")
    common = {
        "schema_version": 1,
        "mode": "PREVIEW_ONLY",
        "real_wechat_write_enabled": False,
        "source_manifest_sha256": manifest["manifest_sha256"],
        "quote_date": "2026-09-23",
    }
    write_json(daily / "wechat_pdf_favorite_payload.json", {
        **common,
        "favorite_kind": "PDF",
        "title": "MIKI HOUSE 9月23日报价｜PDF版",
        "body": "PDF正文",
        "attachments": [pdf_name],
        "attachment_strategy": "FULL_CATALOG",
    })
    write_json(daily / "wechat_text_favorite_payload.json", {
        **common,
        "favorite_kind": "TEXT",
        "title": "MIKI HOUSE 9月23日报价｜文字版",
        "body": "【鞋类】\n10-0001-001｜100元｜赤:13\n",
        "attachments": [],
        "text_format": "LOSSLESS_COMPACT",
        "capacity_readiness": "PASS_70661_SAVED_REOPENED_FULL_HASH",
    })
    write_json(daily / "daily_quote_stats.json", {
        "manifest_sha256": manifest["manifest_sha256"],
        "favorite_preview_count": 2,
        "wechat_real_write_count": 0,
        "shijiu_request_count": 0,
        "shijiu_mutation_count": 0,
        "writer_mutex_evidence_count": 0,
    })
    write_json(daily / "PDF压缩检索验收报告.json", {
        "pdf": {"validation": {"status": "PASS", "search_pass_rate": 1.0}}
    })
    config = {
        "runtime_validation_status": "PASS",
        "validated_manifest_sha256": "14d1da632a8c50f417c6d8b068d4a0ac58859e647cff048ef54e999328272b43",
        "validated_text_format": "LOSSLESS_COMPACT",
        "readback_diagnosis_status": "PASS_70661_SAVED_REOPENED_FULL_HASH",
        "runtime_readiness_evidence_path": "outputs/daily_quote/2026-09-22/wechat_runtime_readiness.json",
        "allow_fresh_daily_manifest_binding": True,
        "max_verified_text_characters": 70661,
        "max_verified_text_utf8_bytes": 96870,
        "max_verified_text_lines": 1794,
        "production_save_enabled": True,
    }
    return daily, config


def test_tracked_production_orchestration_defaults_to_zero_write() -> None:
    config = json.loads(
        (ROOT / "config" / "wechat_favorite_runtime.json").read_text(encoding="utf-8")
    )
    assert config["runtime_validation_status"] == "PASS"
    assert config["allow_fresh_daily_manifest_binding"] is True
    assert config["production_save_enabled"] is False
    code = (ROOT / "src" / "mikihouse_luyao" / "wechat_daily_production.py").read_text(
        encoding="utf-8"
    )
    assert "/shopapi/" not in code


class FakeSink:
    def __init__(self, fail_at: int | None = None) -> None:
        self.calls: list[dict] = []
        self.fail_at = fail_at

    def save(self, payload: dict, **kwargs: object) -> dict:
        self.calls.append({"payload": payload, "kwargs": kwargs})
        if self.fail_at == len(self.calls):
            raise RuntimeError("simulated unknown mutation result")
        return {
            "status": "PASS",
            "gate": {"mode": "PRODUCTION"},
            "comparison": {"normalized_hash_match": True},
        }


def test_bundle_preflight_binds_one_manifest_and_two_validated_payloads(tmp_path: Path) -> None:
    daily, config = build_bundle(tmp_path)
    report = validate_daily_production_bundle(
        daily, config, repository_root=ROOT, require_write_enabled=True
    )
    assert report["status"] == "PRODUCTION_BUNDLE_PREFLIGHT_PASS"
    assert report["quote_date"] == "2026-09-23"
    assert set(report["payloads"]) == {"pdf", "text"}
    assert report["payloads"]["pdf"]["attachments"][0].endswith("报价全集.pdf")
    assert report["shijiu_request_count"] == 0


def test_bundle_preflight_fails_closed_when_default_production_switch_is_off(tmp_path: Path) -> None:
    daily, config = build_bundle(tmp_path)
    config["production_save_enabled"] = False
    with pytest.raises(WeChatDailyProductionError, match="disabled"):
        validate_daily_production_bundle(
            daily, config, repository_root=ROOT, require_write_enabled=True
        )


def test_bundle_preflight_rejects_text_larger_than_verified_capacity(tmp_path: Path) -> None:
    daily, config = build_bundle(tmp_path)
    config["max_verified_text_characters"] = 10
    with pytest.raises(WeChatDailyProductionError, match="exceeds verified"):
        validate_daily_production_bundle(
            daily, config, repository_root=ROOT, require_write_enabled=True
        )


def test_pdf_file_picker_recovery_contract_is_exact_and_read_only(tmp_path: Path) -> None:
    attachment = tmp_path / "MIKIHOUSE_2026-09-23_报价全集.pdf"
    attachment.write_bytes(b"%PDF-verified")
    title = "MIKI HOUSE 9月23日报价｜PDF版"
    evidence = {
        "status": "PASS",
        "recovery_mode": PDF_FILE_PICKER_RECOVERY_MODE,
        "automatic_retry_count": 0,
        "attachment": {
            "filename": attachment.name,
            "byte_count": attachment.stat().st_size,
            "sha256": hashlib.sha256(attachment.read_bytes()).hexdigest(),
            "selected_path": str(attachment.resolve()),
            "visible_before_save": True,
            "visible_after_reopen": True,
            "clipboard_placeholder_after_reopen": "[文件]",
        },
        "text_readback": {"status": "EXACT_READBACK_MATCH", "truncated": False},
        "saved_note_search": {
            "search_candidate_count": 1,
            "search_marker_sha256": hashlib.sha256(title.encode("utf-8")).hexdigest(),
        },
        "chat_send_count": 0,
        "shijiu_request_count": 0,
    }
    result = validate_pdf_file_picker_recovery_evidence(
        evidence, attachment_path=attachment, expected_title=title
    )
    assert result["status"] == "PASS"
    assert result["strategy"] == PDF_FILE_PICKER_RECOVERY_MODE
    assert result["automatic_retry_count"] == 0

    evidence["automatic_retry_count"] = 1
    with pytest.raises(WeChatDailyProductionError, match="automatic retry"):
        validate_pdf_file_picker_recovery_evidence(
            evidence, attachment_path=attachment, expected_title=title
        )


def test_two_favorite_flow_is_ordered_checkpointed_and_idempotent(tmp_path: Path) -> None:
    daily, config = build_bundle(tmp_path)
    sink = FakeSink()
    result = save_daily_production_favorites(
        daily,
        config,
        repository_root=ROOT,
        confirmation=PRODUCTION_CONFIRMATION,
        sink=sink,  # type: ignore[arg-type]
    )
    assert result["status"] == "PASS"
    assert [row["payload"]["favorite_kind"] for row in sink.calls] == ["PDF", "TEXT"]
    assert all(
        row["kwargs"]["authorized_manifest_sha256"] == result["manifest_sha256"]
        for row in sink.calls
    )
    checkpoint = json.loads((daily / CHECKPOINT_FILENAME).read_text())
    assert checkpoint["status"] == "PASS"
    assert checkpoint["favorite_create_count"] == 2
    assert checkpoint["automatic_mutation_retry_count"] == 0
    assert (daily / REPORT_FILENAME).is_file()

    replay = save_daily_production_favorites(
        daily,
        config,
        repository_root=ROOT,
        confirmation=PRODUCTION_CONFIRMATION,
        sink=sink,  # type: ignore[arg-type]
    )
    assert replay["idempotent_replay"] is True
    assert len(sink.calls) == 2


def test_clean_interstage_resume_skips_passed_pdf_and_creates_only_text(tmp_path: Path) -> None:
    daily, config = build_bundle(tmp_path)
    preflight = validate_daily_production_bundle(
        daily, config, repository_root=ROOT, require_write_enabled=True
    )
    checkpoint = _new_checkpoint(preflight)
    checkpoint["status"] = "IN_PROGRESS"
    checkpoint["favorite_create_count"] = 1
    checkpoint["stages"]["pdf"].update({
        "status": "PASS",
        "evidence_file": "wechat_pdf_favorite_production_validation.json",
        "mutation_attempt_count": 1,
    })
    write_json(daily / CHECKPOINT_FILENAME, checkpoint)
    write_json(
        daily / "wechat_pdf_favorite_production_validation.json",
        {"status": "PASS"},
    )
    sink = FakeSink()
    result = save_daily_production_favorites(
        daily,
        config,
        repository_root=ROOT,
        confirmation=PRODUCTION_CONFIRMATION,
        sink=sink,  # type: ignore[arg-type]
    )
    assert result["status"] == "PASS"
    assert [row["payload"]["favorite_kind"] for row in sink.calls] == ["TEXT"]
    final_checkpoint = json.loads((daily / CHECKPOINT_FILENAME).read_text())
    assert final_checkpoint["favorite_create_count"] == 2
    assert final_checkpoint["stages"]["pdf"]["mutation_attempt_count"] == 1
    assert final_checkpoint["stages"]["text"]["mutation_attempt_count"] == 1


def test_checkpoint_refuses_changed_bundle_before_any_additional_mutation(tmp_path: Path) -> None:
    daily, config = build_bundle(tmp_path)
    sink = FakeSink()
    save_daily_production_favorites(
        daily,
        config,
        repository_root=ROOT,
        confirmation=PRODUCTION_CONFIRMATION,
        sink=sink,  # type: ignore[arg-type]
    )
    text_path = daily / "wechat_text_favorite_payload.json"
    text = json.loads(text_path.read_text())
    text["body"] += "篡改"
    write_json(text_path, text)
    with pytest.raises(WeChatDailyProductionError, match="bundle_sha256"):
        save_daily_production_favorites(
            daily,
            config,
            repository_root=ROOT,
            confirmation=PRODUCTION_CONFIRMATION,
            sink=sink,  # type: ignore[arg-type]
        )
    assert len(sink.calls) == 2


def test_any_mutation_uncertainty_freezes_without_retry_or_next_stage(tmp_path: Path) -> None:
    daily, config = build_bundle(tmp_path)
    sink = FakeSink(fail_at=1)
    with pytest.raises(WeChatDailyProductionError, match="frozen without retry"):
        save_daily_production_favorites(
            daily,
            config,
            repository_root=ROOT,
            confirmation=PRODUCTION_CONFIRMATION,
            sink=sink,  # type: ignore[arg-type]
        )
    checkpoint = json.loads((daily / CHECKPOINT_FILENAME).read_text())
    assert checkpoint["status"] == "FROZEN_RECONCILIATION_REQUIRED"
    assert checkpoint["stages"]["pdf"]["mutation_attempt_count"] == 1
    assert checkpoint["stages"]["text"]["status"] == "PENDING"
    assert len(sink.calls) == 1
    with pytest.raises(WeChatDailyProductionError, match="reconciliation required"):
        save_daily_production_favorites(
            daily,
            config,
            repository_root=ROOT,
            confirmation=PRODUCTION_CONFIRMATION,
            sink=sink,  # type: ignore[arg-type]
        )
    assert len(sink.calls) == 1


def test_real_one_click_cli_is_blocked_before_crawl_with_tracked_default_config() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_mikihouse_daily_production.py"),
            "--production-save",
            "--confirm",
            PRODUCTION_CONFIRMATION,
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    evidence = json.loads(result.stdout)
    assert evidence == {
        "status": "FAILED_CLOSED",
        "phase": "PRE_GENERATION_WRITE_GATE",
        "error": "production save config switch is disabled",
        "website_crawl_started": False,
        "wechat_mutation_count": 0,
    }


def test_mac_one_click_entry_is_executable_and_uses_only_gated_runner() -> None:
    command = ROOT / "scripts" / "生成并保存MIKIHOUSE每日两个微信收藏.command"
    runner = ROOT / "scripts" / "run_mikihouse_daily_production.py"
    assert command.is_file() and os.access(command, os.X_OK)
    assert runner.is_file() and os.access(runner, os.X_OK)
    source = command.read_text(encoding="utf-8")
    assert str(runner.name) in source
    assert "--production-save" in source
    assert PRODUCTION_CONFIRMATION in source
    assert "save_wechat_daily_quote_favorite.py" not in source


def test_tracked_pdf_file_picker_recovery_is_fail_closed() -> None:
    config = json.loads(
        (ROOT / "config" / "wechat_favorite_runtime.json").read_text(encoding="utf-8")
    )
    recovery = config["pdf_attachment_recovery"]
    assert recovery == {
        "strategy": PDF_FILE_PICKER_RECOVERY_MODE,
        "requires_explicit_operator_authorization": True,
        "same_open_draft_only": True,
        "single_attempt_only": True,
        "automatic_retry_enabled": False,
        "exact_attachment_path_required": True,
        "pre_save_attachment_visibility_required": True,
        "post_save_unique_title_and_attachment_readback_required": True,
    }
