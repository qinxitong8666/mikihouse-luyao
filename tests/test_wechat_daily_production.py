from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import mikihouse_luyao.wechat_daily_production as production
from mikihouse_luyao.daily_quote import sha256_json
from mikihouse_luyao.wechat_daily_production import (
    CHECKPOINT_FILENAME,
    PDF_FILE_PICKER_RECOVERY_MODE,
    REBUILD_HISTORY_DIRECTORY,
    REPORT_FILENAME,
    WeChatDailyProductionError,
    _new_checkpoint,
    audit_completed_daily_favorites_readonly,
    audit_frozen_pdf_recovery_readonly,
    recover_frozen_pdf_and_complete_daily_favorites,
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
        "production_authorization_mode": "APP_ONE_TIME",
        "production_authorization_operation": "REBUILD_MISSING_DAILY_TWO_FAVORITES",
        "pdf_attachment_recovery": {
            "strategy": PDF_FILE_PICKER_RECOVERY_MODE,
            "requires_explicit_operator_authorization": True,
            "single_attempt_only": True,
            "automatic_retry_enabled": False,
        },
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
    def __init__(
        self,
        fail_at: int | None = None,
        *,
        title_counts: list[int] | None = None,
    ) -> None:
        self.calls: list[dict] = []
        self.fail_at = fail_at
        self.title_counts = title_counts or [0, 0]
        self.title_audit_count = 0
        self.recovery_calls: list[dict] = []

    def save(self, payload: dict, **kwargs: object) -> dict:
        self.calls.append({"payload": payload, "kwargs": kwargs})
        if self.fail_at == len(self.calls):
            raise RuntimeError("simulated unknown mutation result")
        return {
            "status": "PASS",
            "gate": {"mode": "PRODUCTION"},
            "comparison": {"normalized_hash_match": True},
        }

    def audit_titles_read_only(self, titles: list[str]) -> dict:
        self.title_audit_count += 1
        assert len(titles) == 2
        return {
            "status": "READ_ONLY_TITLE_AUDIT_COMPLETE",
            "results": [
                {
                    "title_sha256": hashlib.sha256(title.encode()).hexdigest(),
                    "search_candidate_count": count,
                    "status": "SAVED_NOTE_CANDIDATES_READ_ONLY",
                }
                for title, count in zip(titles, self.title_counts)
            ],
            "all_titles_absent": self.title_counts == [0, 0],
            "wechat_mutation_count": 0,
        }

    def recover_existing_pdf_attachment(self, payload: dict, **kwargs: object) -> dict:
        self.recovery_calls.append({"payload": payload, "kwargs": kwargs})
        attachment = Path(payload["attachments"][0])
        title = payload["title"]
        return {
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
                "clipboard_placeholder_after_reopen": "[\u6587\u4ef6]",
            },
            "text_readback": {"status": "EXACT_READBACK_MATCH", "truncated": False},
            "saved_note_search": {
                "search_candidate_count": 1,
                "search_marker_sha256": hashlib.sha256(title.encode("utf-8")).hexdigest(),
            },
            "chat_send_count": 0,
            "shijiu_request_count": 0,
        }

    def audit_pdf_draft_read_only(self, payload):
        return {"status": "VERIFIED_PAYLOAD_DRAFT_WITHOUT_ATTACHMENT",
                "attachment_marker_count": 0, "comparison": {"normalized_hash_match": True}}


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


def test_safe_rebuild_requires_both_titles_absent_and_archives_previous_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(production, "_today", lambda: "2026-09-23")
    daily, config = build_bundle(tmp_path)
    first = FakeSink()
    save_daily_production_favorites(
        daily,
        config,
        repository_root=ROOT,
        confirmation=PRODUCTION_CONFIRMATION,
        sink=first,  # type: ignore[arg-type]
    )
    old_checkpoint = (daily / CHECKPOINT_FILENAME).read_bytes()
    rebuild = FakeSink(title_counts=[0, 0])
    result = save_daily_production_favorites(
        daily,
        config,
        repository_root=ROOT,
        confirmation=PRODUCTION_CONFIRMATION,
        sink=rebuild,  # type: ignore[arg-type]
        rebuild_missing_daily=True,
    )
    assert result["status"] == "PASS"
    assert result["safe_rebuild"]["status"] == "AUTHORIZED_RESET_AFTER_BOTH_TITLES_ABSENT"
    assert rebuild.title_audit_count == 1
    assert [row["payload"]["favorite_kind"] for row in rebuild.calls] == ["PDF", "TEXT"]
    archives = list((daily / REBUILD_HISTORY_DIRECTORY).iterdir())
    assert len(archives) == 1
    assert (archives[0] / CHECKPOINT_FILENAME).read_bytes() == old_checkpoint
    archive_manifest = json.loads((archives[0] / "archive_manifest.json").read_text())
    assert archive_manifest["status"] == "PREVIOUS_PASS_ARCHIVED_BEFORE_SAFE_REBUILD"
    assert archive_manifest["wechat_mutation_count"] == 0


@pytest.mark.parametrize("counts", ([1, 0], [0, 1], [1, 1]))
def test_safe_rebuild_blocks_if_either_title_still_exists(
    tmp_path: Path,
    counts: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(production, "_today", lambda: "2026-09-23")
    daily, config = build_bundle(tmp_path)
    first = FakeSink()
    save_daily_production_favorites(
        daily,
        config,
        repository_root=ROOT,
        confirmation=PRODUCTION_CONFIRMATION,
        sink=first,  # type: ignore[arg-type]
    )
    checkpoint_before = (daily / CHECKPOINT_FILENAME).read_bytes()
    report_before = (daily / REPORT_FILENAME).read_bytes()
    rebuild = FakeSink(title_counts=counts)
    audit = audit_completed_daily_favorites_readonly(
        daily,
        config,
        sink=rebuild,  # type: ignore[arg-type]
        require_today=False,
    )
    assert audit["status"] == "BLOCKED_EXISTING_DAILY_FAVORITE"
    with pytest.raises(WeChatDailyProductionError, match="still exist"):
        save_daily_production_favorites(
            daily,
            config,
            repository_root=ROOT,
            confirmation=PRODUCTION_CONFIRMATION,
            sink=rebuild,  # type: ignore[arg-type]
            rebuild_missing_daily=True,
        )
    assert rebuild.calls == []
    assert (daily / CHECKPOINT_FILENAME).read_bytes() == checkpoint_before
    assert (daily / REPORT_FILENAME).read_bytes() == report_before
    assert not (daily / REBUILD_HISTORY_DIRECTORY).exists()


def test_safe_rebuild_without_completed_checkpoint_fails_before_mutation(
    tmp_path: Path,
) -> None:
    daily, config = build_bundle(tmp_path)
    sink = FakeSink()
    with pytest.raises(WeChatDailyProductionError, match="existing completed"):
        save_daily_production_favorites(
            daily,
            config,
            repository_root=ROOT,
            confirmation=PRODUCTION_CONFIRMATION,
            sink=sink,  # type: ignore[arg-type]
            rebuild_missing_daily=True,
        )
    assert sink.calls == []
    assert sink.title_audit_count == 0


def frozen_record_with_regenerated_bundle(tmp_path: Path):
    daily, config = build_bundle(tmp_path)
    with pytest.raises(WeChatDailyProductionError):
        save_daily_production_favorites(
            daily, config, repository_root=ROOT, confirmation=PRODUCTION_CONFIRMATION,
            sink=FakeSink(fail_at=1),
        )
    checkpoint_bytes = (daily / CHECKPOINT_FILENAME).read_bytes()
    old = json.loads(checkpoint_bytes)
    manifest_path = daily / "daily_quote_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.pop("manifest_sha256")
    manifest["generated_at"] = "2026-09-23T22:50:00+09:00"
    manifest["manifest_sha256"] = sha256_json(manifest)
    write_json(manifest_path, manifest)
    for name in ("daily_quote_stats.json", "wechat_pdf_favorite_payload.json", "wechat_text_favorite_payload.json"):
        path = daily / name
        value = json.loads(path.read_text())
        key = "manifest_sha256" if name == "daily_quote_stats.json" else "source_manifest_sha256"
        value[key] = manifest["manifest_sha256"]
        write_json(path, value)
    write_json(daily / "wechat_pdf_failure_validation.json", {"old_manifest": old["manifest_sha256"], "status": "FAILED"})
    return daily, config, checkpoint_bytes, manifest["manifest_sha256"]


def test_frozen_mismatched_manifest_rebuild_archives_old_and_binds_current(tmp_path, monkeypatch):
    monkeypatch.setattr(production, "_today", lambda: "2026-09-23")
    daily, config, old_checkpoint, current_manifest = frozen_record_with_regenerated_bundle(tmp_path)
    current_bytes = (daily / "daily_quote_manifest.json").read_bytes()
    sink = FakeSink(title_counts=[0, 0])
    audit = audit_completed_daily_favorites_readonly(daily, config, sink=sink)
    assert audit["status"] == "READY_FOR_EXPLICIT_REBUILD_AUTHORIZATION"
    assert audit["prior_checkpoint_status"] == "FROZEN_RECONCILIATION_REQUIRED"
    assert (daily / CHECKPOINT_FILENAME).read_bytes() == old_checkpoint
    result = save_daily_production_favorites(
        daily, config, repository_root=ROOT, confirmation=PRODUCTION_CONFIRMATION,
        sink=sink, rebuild_missing_daily=True,
    )
    assert result["status"] == "PASS"
    assert result["manifest_sha256"] == current_manifest
    assert (daily / "daily_quote_manifest.json").read_bytes() == current_bytes
    archive, = (daily / REBUILD_HISTORY_DIRECTORY).iterdir()
    assert (archive / CHECKPOINT_FILENAME).read_bytes() == old_checkpoint
    assert (archive / "wechat_pdf_failure_validation.json").read_bytes() == (daily / "wechat_pdf_failure_validation.json").read_bytes()
    history = json.loads((archive / "archive_manifest.json").read_text())
    assert history["status"] == "PREVIOUS_FROZEN_ARCHIVED_BEFORE_SAFE_REBUILD"
    assert history["previous_bundle_matches_current"] is False
    assert len(sink.calls) == 2
    # Ordinary replay consumes no extra mutation, archive or generation.
    replay = save_daily_production_favorites(daily, config, repository_root=ROOT, confirmation=PRODUCTION_CONFIRMATION, sink=sink)
    assert replay["idempotent_replay"] and len(sink.calls) == 2


@pytest.mark.parametrize("counts", [[1, 0], [0, 1], [1, 1], [2, 0]])
def test_frozen_rebuild_existing_note_never_resets_or_overwrites(tmp_path, monkeypatch, counts):
    monkeypatch.setattr(production, "_today", lambda: "2026-09-23")
    daily, config, _, _ = frozen_record_with_regenerated_bundle(tmp_path)
    before = {p.name: p.read_bytes() for p in daily.iterdir() if p.is_file()}
    sink = FakeSink(title_counts=counts)
    with pytest.raises(WeChatDailyProductionError, match="still exist"):
        save_daily_production_favorites(daily, config, repository_root=ROOT, confirmation=PRODUCTION_CONFIRMATION, sink=sink, rebuild_missing_daily=True)
    assert not sink.calls
    assert before == {p.name: p.read_bytes() for p in daily.iterdir() if p.is_file()}
    assert not (daily / REBUILD_HISTORY_DIRECTORY).exists()


def test_frozen_rebuild_requires_specific_app_operation(tmp_path):
    daily, config, original, _ = frozen_record_with_regenerated_bundle(tmp_path)
    config["production_authorization_operation"] = "CREATE_DAILY_TWO_FAVORITES"
    sink = FakeSink()
    with pytest.raises(WeChatDailyProductionError, match="专用单次授权"):
        save_daily_production_favorites(daily, config, repository_root=ROOT, confirmation=PRODUCTION_CONFIRMATION, sink=sink, rebuild_missing_daily=True)
    assert not sink.calls and sink.title_audit_count == 0
    assert (daily / CHECKPOINT_FILENAME).read_bytes() == original


def test_frozen_archive_failure_preserves_checkpoint_before_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(production, "_today", lambda: "2026-09-23")
    daily, config, original, _ = frozen_record_with_regenerated_bundle(tmp_path)
    def fail_copy(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(production.shutil, "copy2", fail_copy)
    sink = FakeSink()
    with pytest.raises(OSError, match="disk full"):
        save_daily_production_favorites(daily, config, repository_root=ROOT, confirmation=PRODUCTION_CONFIRMATION, sink=sink, rebuild_missing_daily=True)
    assert not sink.calls
    assert (daily / CHECKPOINT_FILENAME).read_bytes() == original


def test_frozen_bundle_changed_during_absence_audit_blocks_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(production, "_today", lambda: "2026-09-23")
    daily, config, original, _ = frozen_record_with_regenerated_bundle(tmp_path)
    class ChangingSink(FakeSink):
        def audit_titles_read_only(self, titles):
            evidence = super().audit_titles_read_only(titles)
            (daily / "MIKIHOUSE_2026-09-23_报价全集.pdf").write_bytes(b"changed PDF")
            return evidence
    sink = ChangingSink()
    with pytest.raises(WeChatDailyProductionError, match="bundle changed"):
        save_daily_production_favorites(daily, config, repository_root=ROOT, confirmation=PRODUCTION_CONFIRMATION, sink=sink, rebuild_missing_daily=True)
    assert not sink.calls
    assert (daily / CHECKPOINT_FILENAME).read_bytes() == original


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


def _write_canonical_frozen_attachment_checkpoint(
    daily: Path, config: dict
) -> None:
    preflight = validate_daily_production_bundle(
        daily, config, repository_root=ROOT, require_write_enabled=True
    )
    checkpoint = _new_checkpoint(preflight)
    checkpoint["status"] = "FROZEN_RECONCILIATION_REQUIRED"
    checkpoint["stages"]["pdf"].update(
        {
            "status": "FROZEN_AFTER_MUTATION_ATTEMPT",
            "mutation_attempt_count": 1,
            "error_type": "WeChatRuntimeError",
            "error": "attachment was not visible before save",
        }
    )
    write_json(daily / CHECKPOINT_FILENAME, checkpoint)
    write_json(
        daily / REPORT_FILENAME,
        {
            "status": "FROZEN_RECONCILIATION_REQUIRED",
            "quote_date": daily.name,
            "manifest_sha256": preflight["manifest_sha256"],
            "bundle_sha256": preflight["bundle_sha256"],
        },
    )


@pytest.mark.parametrize("error", ["attachment was not visible before save", "post-readback note title does not match verified body"])
def test_frozen_pdf_recovery_requires_one_pdf_zero_text_and_completes_once(
    tmp_path: Path, error: str,
) -> None:
    daily, config = build_bundle(tmp_path)
    _write_canonical_frozen_attachment_checkpoint(daily, config)
    frozen = json.loads((daily / CHECKPOINT_FILENAME).read_text())
    frozen["stages"]["pdf"]["error"] = error
    write_json(daily / CHECKPOINT_FILENAME, frozen)
    sink = FakeSink(title_counts=[1, 0])
    audit = audit_frozen_pdf_recovery_readonly(
        daily,
        config,
        repository_root=ROOT,
        sink=sink,  # type: ignore[arg-type]
    )
    assert audit["status"] == "READY_FOR_EXPLICIT_PDF_RECOVERY_AUTHORIZATION"
    assert audit["exact_title_counts"] == [1, 0]
    assert audit["wechat_mutation_count"] == 0

    result = recover_frozen_pdf_and_complete_daily_favorites(
        daily,
        config,
        repository_root=ROOT,
        confirmation=PRODUCTION_CONFIRMATION,
        sink=sink,  # type: ignore[arg-type]
    )
    assert result["status"] == "PASS"
    assert result["recovered_existing_pdf_note_without_duplicate_create"] is True
    assert len(sink.recovery_calls) == 1
    assert [row["payload"]["favorite_kind"] for row in sink.calls] == ["TEXT"]
    checkpoint = json.loads((daily / CHECKPOINT_FILENAME).read_text())
    assert checkpoint["status"] == "PASS"
    assert checkpoint["favorite_create_count"] == 2
    assert checkpoint["stages"]["pdf"]["mutation_attempt_count"] == 1
    assert checkpoint["stages"]["pdf"]["authorized_recovery"]["mutation_attempt_count"] == 1
    assert checkpoint["stages"]["text"]["mutation_attempt_count"] == 1


@pytest.mark.parametrize("counts", ([0, 0], [1, 1], [2, 0]))
def test_frozen_pdf_recovery_wrong_title_state_fails_before_mutation(
    tmp_path: Path, counts: list[int]
) -> None:
    daily, config = build_bundle(tmp_path)
    _write_canonical_frozen_attachment_checkpoint(daily, config)
    sink = FakeSink(title_counts=counts)
    checkpoint_before = (daily / CHECKPOINT_FILENAME).read_bytes()
    with pytest.raises(WeChatDailyProductionError, match="one PDF title and zero text"):
        recover_frozen_pdf_and_complete_daily_favorites(
            daily,
            config,
            repository_root=ROOT,
            confirmation=PRODUCTION_CONFIRMATION,
            sink=sink,  # type: ignore[arg-type]
        )
    assert sink.recovery_calls == []
    assert sink.calls == []
    assert (daily / CHECKPOINT_FILENAME).read_bytes() == checkpoint_before


def test_unknown_frozen_error_is_not_a_recovery_permission(tmp_path):
    daily, config = build_bundle(tmp_path)
    _write_canonical_frozen_attachment_checkpoint(daily, config)
    frozen = json.loads((daily / CHECKPOINT_FILENAME).read_text())
    frozen["stages"]["pdf"]["error"] = "unknown outcome after AXOpen"
    write_json(daily / CHECKPOINT_FILENAME, frozen)
    sink = FakeSink(title_counts=[1, 0])
    with pytest.raises(WeChatDailyProductionError, match="canonical attachment failure"):
        audit_frozen_pdf_recovery_readonly(daily, config, repository_root=ROOT, sink=sink)
    assert sink.calls == [] and sink.recovery_calls == []


def test_title_scoped_recovery_preserves_consumed_attempt_and_cannot_repeat(tmp_path):
    daily, config = build_bundle(tmp_path)
    _write_canonical_frozen_attachment_checkpoint(daily, config)
    frozen = json.loads((daily / CHECKPOINT_FILENAME).read_text())
    stage = frozen["stages"]["pdf"]
    stage["status"] = "FROZEN_AFTER_RECOVERY_MUTATION_ATTEMPT"
    prior = {"status": "FROZEN_AFTER_RECOVERY_MUTATION_ATTEMPT", "mutation_attempt_count": 1,
             "mutation_started_at": "original", "error": "expected one owned note window, got 2"}
    stage["authorized_recovery"] = prior
    write_json(daily / CHECKPOINT_FILENAME, frozen)
    sink = FakeSink(title_counts=[1, 0])
    result = recover_frozen_pdf_and_complete_daily_favorites(daily, config, repository_root=ROOT,
              confirmation=PRODUCTION_CONFIRMATION, sink=sink)
    assert result["status"] == "PASS"
    final = json.loads((daily / CHECKPOINT_FILENAME).read_text())
    assert final["stages"]["pdf"]["recovery_history"] == [prior]
    assert len(sink.recovery_calls) == 1 and len(sink.calls) == 1
    with pytest.raises(WeChatDailyProductionError):
        recover_frozen_pdf_and_complete_daily_favorites(daily, config, repository_root=ROOT,
                  confirmation=PRODUCTION_CONFIRMATION, sink=sink)
    assert len(sink.recovery_calls) == 1


def test_title_scoped_resume_cannot_replay_another_failure(tmp_path):
    daily, config = build_bundle(tmp_path)
    _write_canonical_frozen_attachment_checkpoint(daily, config)
    frozen = json.loads((daily / CHECKPOINT_FILENAME).read_text())
    frozen["stages"]["pdf"].update(status="FROZEN_AFTER_RECOVERY_MUTATION_ATTEMPT",
         authorized_recovery={"status":"FROZEN_AFTER_RECOVERY_MUTATION_ATTEMPT", "error":"unknown upload", "mutation_attempt_count":1})
    write_json(daily / CHECKPOINT_FILENAME, frozen)
    sink = FakeSink(title_counts=[1, 0])
    with pytest.raises(WeChatDailyProductionError):
        audit_frozen_pdf_recovery_readonly(daily, config, repository_root=ROOT, sink=sink)
    assert sink.recovery_calls == [] and sink.title_audit_count == 0


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
        "error": "仓库默认生产开关关闭；请从 MIKI HOUSE 报价助手.app 完成本次单次授权。",
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
    assert config["production_pdf_attachment"] == {
        "strategy": "TOOLBAR_FILE_PICKER_SINGLE_ATTEMPT",
        "single_attempt_only": True,
        "automatic_retry_enabled": False,
        "exact_attachment_path_required": True,
        "pre_save_attachment_visibility_required": True,
        "post_save_unique_title_and_attachment_readback_required": True,
    }
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
