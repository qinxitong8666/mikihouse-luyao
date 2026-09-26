from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .daily_quote import sha256_json
from .daily_quote_guard import locked_daily_output
from .wechat_favorite_runtime import (
    MacWeChatFavoriteSink,
    PRODUCTION_CONFIRMATION,
    text_fingerprint,
    write_json,
)


CHECKPOINT_FILENAME = "wechat_daily_production_checkpoint.json"
REPORT_FILENAME = "wechat_daily_production_report.json"
REBUILD_HISTORY_DIRECTORY = "wechat_daily_production_rebuild_history"
STAGE_ORDER = ("pdf", "text")
PDF_FILE_PICKER_RECOVERY_MODE = (
    "OPERATOR_AUTHORIZED_TOOLBAR_FILE_PICKER_SINGLE_ATTEMPT"
)


class WeChatDailyProductionError(RuntimeError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Tokyo")).isoformat()


def _today() -> str:
    return datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()


def _resolve_payload_attachments(
    daily_dir: Path, payload: dict[str, Any]
) -> dict[str, Any]:
    resolved = dict(payload)
    attachments: list[str] = []
    for value in payload.get("attachments") or []:
        path = Path(str(value))
        if not path.is_absolute():
            path = daily_dir / path
        attachments.append(str(path.resolve()))
    resolved["attachments"] = attachments
    return resolved


def validate_pdf_file_picker_recovery_evidence(
    evidence: dict[str, Any],
    *,
    attachment_path: Path,
    expected_title: str,
) -> dict[str, Any]:
    """Validate the one audited PDF picker recovery without touching WeChat.

    This is deliberately an evidence contract, not a second automated mutation
    path.  The toolbar picker may only be used on the already-open draft after
    explicit operator authorization, exactly once, with no automatic retry.
    """

    attachment_path = attachment_path.resolve()
    if evidence.get("status") != "PASS":
        raise WeChatDailyProductionError("PDF file-picker recovery evidence is not PASS")
    if evidence.get("recovery_mode") != PDF_FILE_PICKER_RECOVERY_MODE:
        raise WeChatDailyProductionError("PDF file-picker recovery mode is not canonical")
    if int(evidence.get("automatic_retry_count") or 0) != 0:
        raise WeChatDailyProductionError("PDF file-picker recovery used automatic retry")
    attachment = evidence.get("attachment") or {}
    if Path(str(attachment.get("selected_path") or "")).resolve() != attachment_path:
        raise WeChatDailyProductionError("PDF file-picker recovery selected a different path")
    if attachment.get("filename") != attachment_path.name:
        raise WeChatDailyProductionError("PDF file-picker recovery filename mismatch")
    if int(attachment.get("byte_count") or 0) != attachment_path.stat().st_size:
        raise WeChatDailyProductionError("PDF file-picker recovery byte count mismatch")
    actual_sha256 = hashlib.sha256(attachment_path.read_bytes()).hexdigest()
    if attachment.get("sha256") != actual_sha256:
        raise WeChatDailyProductionError("PDF file-picker recovery attachment hash mismatch")
    if attachment.get("visible_before_save") is not True:
        raise WeChatDailyProductionError("PDF file-picker recovery lacks pre-save visibility proof")
    if attachment.get("visible_after_reopen") is not True:
        raise WeChatDailyProductionError("PDF file-picker recovery lacks reopened attachment proof")
    if attachment.get("clipboard_placeholder_after_reopen") != "[文件]":
        raise WeChatDailyProductionError("PDF file-picker recovery placeholder mismatch")
    search = evidence.get("saved_note_search") or {}
    if int(search.get("search_candidate_count") or 0) != 1:
        raise WeChatDailyProductionError("PDF file-picker recovery title is not unique")
    expected_title_sha256 = hashlib.sha256(expected_title.encode("utf-8")).hexdigest()
    if search.get("search_marker_sha256") != expected_title_sha256:
        raise WeChatDailyProductionError("PDF file-picker recovery title hash mismatch")
    text_readback = evidence.get("text_readback") or {}
    if (
        text_readback.get("status") != "EXACT_READBACK_MATCH"
        or text_readback.get("truncated") is not False
    ):
        raise WeChatDailyProductionError("PDF file-picker recovery text readback failed")
    if any(int(evidence.get(key) or 0) != 0 for key in ("chat_send_count", "shijiu_request_count")):
        raise WeChatDailyProductionError("PDF file-picker recovery crossed a forbidden boundary")
    return {
        "status": "PASS",
        "strategy": PDF_FILE_PICKER_RECOVERY_MODE,
        "attachment_filename": attachment_path.name,
        "attachment_byte_count": attachment_path.stat().st_size,
        "attachment_sha256": actual_sha256,
        "title_sha256": expected_title_sha256,
        "saved_note_candidate_count": 1,
        "automatic_retry_count": 0,
        "chat_send_count": 0,
        "shijiu_request_count": 0,
    }


def validate_daily_production_bundle(
    daily_dir: Path,
    runtime_config: dict[str, Any],
    *,
    repository_root: Path,
    require_write_enabled: bool,
) -> dict[str, Any]:
    """Validate one frozen daily bundle without touching WeChat.

    The validated 2026-09-22 manifest proves the runtime capability.  A future
    production run is instead bound to its own freshly generated manifest here;
    this function verifies that both payloads are exact consumers of that one
    manifest before the sink receives an invocation-time binding.
    """

    daily_dir = daily_dir.resolve()
    manifest_path = daily_dir / "daily_quote_manifest.json"
    pdf_path = daily_dir / "wechat_pdf_favorite_payload.json"
    text_path = daily_dir / "wechat_text_favorite_payload.json"
    stats_path = daily_dir / "daily_quote_stats.json"
    pdf_qa_path = daily_dir / "PDF压缩检索验收报告.json"
    for path in (manifest_path, pdf_path, text_path, stats_path, pdf_qa_path):
        if not path.is_file():
            raise WeChatDailyProductionError(f"required daily artifact is missing: {path.name}")

    manifest = _read_json(manifest_path)
    pdf_payload_raw = _read_json(pdf_path)
    text_payload_raw = _read_json(text_path)
    stats = _read_json(stats_path)
    pdf_qa = _read_json(pdf_qa_path)
    manifest_sha256 = str(manifest.get("manifest_sha256") or "")
    recalculated = sha256_json(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    )
    if not manifest_sha256 or recalculated != manifest_sha256:
        raise WeChatDailyProductionError("daily manifest self-hash mismatch")

    quote_date = str(manifest.get("quote_date") or "")
    if daily_dir.name != quote_date:
        raise WeChatDailyProductionError("daily directory date does not match manifest")
    if stats.get("manifest_sha256") != manifest_sha256:
        raise WeChatDailyProductionError("daily stats manifest binding mismatch")
    if stats.get("favorite_preview_count") != 2:
        raise WeChatDailyProductionError("daily run did not produce exactly two Favorite previews")
    if any(
        int(stats.get(key) or 0) != 0
        for key in (
            "wechat_real_write_count",
            "shijiu_request_count",
            "shijiu_mutation_count",
            "writer_mutex_evidence_count",
        )
    ):
        raise WeChatDailyProductionError("daily generation phase contains forbidden mutation evidence")
    pdf_validation = (pdf_qa.get("pdf") or {}).get("validation") or {}
    if (
        pdf_validation.get("status") != "PASS"
        or pdf_validation.get("search_pass_rate") != 1
    ):
        raise WeChatDailyProductionError("PDF automated search/structure validation is not PASS")
    if runtime_config.get("runtime_validation_status") != "PASS":
        raise WeChatDailyProductionError("WeChat runtime validation is not PASS")
    if runtime_config.get("validated_text_format") != "LOSSLESS_COMPACT":
        raise WeChatDailyProductionError("validated WeChat text format is not LOSSLESS_COMPACT")
    if runtime_config.get("readback_diagnosis_status") != "PASS_70661_SAVED_REOPENED_FULL_HASH":
        raise WeChatDailyProductionError("full compact-text reopen/readback evidence is missing")
    if runtime_config.get("allow_fresh_daily_manifest_binding") is not True:
        raise WeChatDailyProductionError("fresh daily manifest binding is disabled")
    if require_write_enabled and runtime_config.get("production_save_enabled") is not True:
        raise WeChatDailyProductionError("production save config switch is disabled")

    readiness_value = str(
        runtime_config.get("runtime_readiness_evidence_path")
        or "outputs/daily_quote/2026-09-22/wechat_runtime_readiness.json"
    )
    readiness_path = Path(readiness_value)
    if not readiness_path.is_absolute():
        readiness_path = repository_root / readiness_path
    readiness = _read_json(readiness_path)
    if (
        readiness.get("status") != "PRODUCTION_READY_TWO_FAVORITES"
        or readiness.get("daily_exactly_two_favorites_production_ready") is not True
        or readiness.get("chat_send_count") != 0
        or readiness.get("shijiu_request_count") != 0
    ):
        raise WeChatDailyProductionError("tracked two-favorite runtime evidence is not production-ready")
    if readiness.get("source_manifest_sha256") != runtime_config.get("validated_manifest_sha256"):
        raise WeChatDailyProductionError("runtime evidence does not match the validated sample manifest")

    payloads_raw = {"pdf": pdf_payload_raw, "text": text_payload_raw}
    payloads: dict[str, dict[str, Any]] = {}
    payload_hashes: dict[str, str] = {}
    expected_suffix = {"pdf": "｜PDF版", "text": "｜文字版"}
    expected_kind = {"pdf": "PDF", "text": "TEXT"}
    for stage in STAGE_ORDER:
        raw = payloads_raw[stage]
        if raw.get("source_manifest_sha256") != manifest_sha256:
            raise WeChatDailyProductionError(f"{stage} payload manifest binding mismatch")
        if raw.get("quote_date") != quote_date:
            raise WeChatDailyProductionError(f"{stage} payload quote date mismatch")
        if raw.get("favorite_kind") != expected_kind[stage]:
            raise WeChatDailyProductionError(f"{stage} payload kind mismatch")
        if raw.get("mode") != "PREVIEW_ONLY" or raw.get("real_wechat_write_enabled") is not False:
            raise WeChatDailyProductionError(f"{stage} payload is not an immutable preview artifact")
        if not str(raw.get("title") or "").endswith(expected_suffix[stage]):
            raise WeChatDailyProductionError(f"{stage} payload title is not canonical")
        payload_hashes[stage] = _canonical_sha256(raw)
        payloads[stage] = _resolve_payload_attachments(daily_dir, raw)

    if text_payload_raw.get("text_format") != "LOSSLESS_COMPACT":
        raise WeChatDailyProductionError("text payload is not LOSSLESS_COMPACT")
    if text_payload_raw.get("capacity_readiness") != "PASS_70661_SAVED_REOPENED_FULL_HASH":
        raise WeChatDailyProductionError("text payload capacity readiness is not PASS")
    text_body = str(text_payload_raw.get("body") or "")
    text_chars = len(text_body)
    text_bytes = len(text_body.encode("utf-8"))
    text_lines = len(text_body.splitlines())
    max_verified = int(runtime_config.get("max_verified_text_characters") or 0)
    if max_verified <= 0 or text_chars > max_verified:
        raise WeChatDailyProductionError(
            f"text payload exceeds verified WeChat capacity: {text_chars}>{max_verified}"
        )
    max_verified_bytes = int(runtime_config.get("max_verified_text_utf8_bytes") or 0)
    if max_verified_bytes <= 0 or text_bytes > max_verified_bytes:
        raise WeChatDailyProductionError(
            f"text payload exceeds verified WeChat byte capacity: {text_bytes}>{max_verified_bytes}"
        )
    max_verified_lines = int(runtime_config.get("max_verified_text_lines") or 0)
    if max_verified_lines <= 0 or text_lines > max_verified_lines:
        raise WeChatDailyProductionError(
            f"text payload exceeds verified WeChat line capacity: {text_lines}>{max_verified_lines}"
        )
    missing_numbers = [
        row["product_number"]
        for row in manifest.get("products") or []
        if row["product_number"] not in text_body
    ]
    if missing_numbers:
        raise WeChatDailyProductionError(
            f"compact text payload is missing {len(missing_numbers)} product numbers"
        )

    pdf_attachments = [Path(value) for value in payloads["pdf"]["attachments"]]
    attachment_strategy = pdf_payload_raw.get("attachment_strategy")
    if attachment_strategy not in {"FULL_CATALOG", "THREE_CATEGORY_PDFS"}:
        raise WeChatDailyProductionError("PDF attachment strategy is unsupported")
    expected_attachment_count = 1 if attachment_strategy == "FULL_CATALOG" else 3
    if len(pdf_attachments) != expected_attachment_count:
        raise WeChatDailyProductionError("PDF attachment strategy/count mismatch")
    attachment_evidence: list[dict[str, Any]] = []
    for path in pdf_attachments:
        if not path.is_file() or path.stat().st_size <= 0:
            raise WeChatDailyProductionError(f"PDF attachment is missing or empty: {path.name}")
        attachment_evidence.append(
            {
                "filename": path.name,
                "byte_count": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    if payloads["text"]["attachments"]:
        raise WeChatDailyProductionError("text favorite must not contain attachments")

    bundle_core = {
        "quote_date": quote_date,
        "manifest_sha256": manifest_sha256,
        "payload_sha256": payload_hashes,
        "attachment_evidence": attachment_evidence,
        "text_body_fingerprint": text_fingerprint(str(text_payload_raw.get("body") or "")),
    }
    return {
        "status": "PRODUCTION_BUNDLE_PREFLIGHT_PASS",
        **bundle_core,
        "bundle_sha256": _canonical_sha256(bundle_core),
        "payloads": payloads,
        "runtime_evidence_status": readiness["status"],
        "production_save_enabled": runtime_config.get("production_save_enabled") is True,
        "shijiu_request_count": 0,
        "chat_send_count": 0,
    }


def _new_checkpoint(preflight: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "READY",
        "quote_date": preflight["quote_date"],
        "manifest_sha256": preflight["manifest_sha256"],
        "bundle_sha256": preflight["bundle_sha256"],
        "created_at": _now(),
        "updated_at": _now(),
        "stages": {
            stage: {
                "status": "PENDING",
                "payload_sha256": preflight["payload_sha256"][stage],
                "mutation_attempt_count": 0,
            }
            for stage in STAGE_ORDER
        },
        "favorite_create_count": 0,
        "automatic_mutation_retry_count": 0,
        "chat_send_count": 0,
        "shijiu_request_count": 0,
    }


def _daily_titles(daily_dir: Path) -> list[str]:
    titles: list[str] = []
    for stage in STAGE_ORDER:
        payload = _read_json(daily_dir / f"wechat_{stage}_favorite_payload.json")
        title = str(payload.get("title") or "")
        if not title:
            raise WeChatDailyProductionError(f"{stage} Favorite title is missing")
        titles.append(title)
    if len(set(titles)) != len(STAGE_ORDER):
        raise WeChatDailyProductionError("daily Favorite titles must be distinct")
    day = datetime.fromisoformat(daily_dir.name)
    expected = [f"MIKI HOUSE {day.month}月{day.day}日报价｜{kind}版" for kind in ("PDF", "文字")]
    if titles != expected:
        raise WeChatDailyProductionError("当天 payload 标题不符合固定日期格式，禁止用变更后的标题证明旧收藏不存在")
    return titles


def _validate_completed_production_record(daily_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    checkpoint_path = daily_dir / CHECKPOINT_FILENAME
    report_path = daily_dir / REPORT_FILENAME
    if not checkpoint_path.is_file() or not report_path.is_file():
        raise WeChatDailyProductionError(
            "safe rebuild requires a completed daily production checkpoint and report"
        )
    checkpoint = _read_json(checkpoint_path)
    report = _read_json(report_path)
    if checkpoint.get("status") != "PASS" or report.get("status") != "PASS":
        raise WeChatDailyProductionError(
            "safe rebuild requires the previous daily production to be PASS"
        )
    if int(checkpoint.get("favorite_create_count") or 0) != 2:
        raise WeChatDailyProductionError("completed checkpoint does not prove exactly two Favorites")
    if checkpoint.get("quote_date") != daily_dir.name or report.get("quote_date") != daily_dir.name:
        raise WeChatDailyProductionError("completed production date does not match the daily directory")
    if report.get("bundle_sha256") != checkpoint.get("bundle_sha256"):
        raise WeChatDailyProductionError("completed checkpoint/report bundle mismatch")
    for stage in STAGE_ORDER:
        if ((checkpoint.get("stages") or {}).get(stage) or {}).get("status") != "PASS":
            raise WeChatDailyProductionError(f"completed checkpoint stage is not PASS: {stage}")
    return checkpoint, report


def _validate_rebuildable_production_record(daily_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    checkpoint = _read_json(daily_dir / CHECKPOINT_FILENAME)
    if checkpoint.get("status") == "PASS":
        return _validate_completed_production_record(daily_dir)
    report = _read_json(daily_dir / REPORT_FILENAME)
    if checkpoint.get("status") != "FROZEN_RECONCILIATION_REQUIRED" or report.get("status") != "FROZEN_RECONCILIATION_REQUIRED":
        raise WeChatDailyProductionError("安全重建仅允许已完成 PASS 或明确冻结的 checkpoint；运行中/未知状态禁止重置")
    for key in ("quote_date", "manifest_sha256", "bundle_sha256"):
        if not checkpoint.get(key) or checkpoint.get(key) != report.get(key):
            raise WeChatDailyProductionError(f"frozen checkpoint/report {key} mismatch")
    if checkpoint["quote_date"] != daily_dir.name:
        raise WeChatDailyProductionError("frozen checkpoint date mismatch")
    # A different *current* bundle is expected in the explicitly authorized
    # migration. The old checkpoint and its own report must still agree.
    return checkpoint, report


def audit_completed_daily_favorites_readonly(
    daily_dir: Path,
    runtime_config: dict[str, Any],
    *,
    sink: MacWeChatFavoriteSink | None = None,
    require_today: bool = True,
) -> dict[str, Any]:
    """Audit PASS or frozen records independently of the current bundle hash."""

    daily_dir = daily_dir.resolve()
    checkpoint, report = _validate_rebuildable_production_record(daily_dir)
    if require_today and daily_dir.name != _today():
        raise WeChatDailyProductionError("safe rebuild is only allowed for today's production")
    titles = _daily_titles(daily_dir)
    target = sink or MacWeChatFavoriteSink(runtime_config)
    evidence = target.audit_titles_read_only(titles)
    rows = list(evidence.get("results") or [])
    if len(rows) != 2:
        raise WeChatDailyProductionError("read-only title audit did not return exactly two results")
    counts = [row.get("search_candidate_count") for row in rows]
    if any(type(count) is not int or count < 0 for count in counts):
        raise WeChatDailyProductionError("read-only title audit returned an invalid count")
    if evidence.get("status") != "READ_ONLY_TITLE_AUDIT_COMPLETE" or any(
        row.get("title_sha256") != hashlib.sha256(title.encode("utf-8")).hexdigest()
        or row.get("status") != "SAVED_NOTE_CANDIDATES_READ_ONLY"
        for title, row in zip(titles, rows)
    ):
        raise WeChatDailyProductionError("read-only title audit identity/status mismatch")
    all_absent = counts == [0, 0] and evidence.get("all_titles_absent") is True
    return {
        "schema_version": 1,
        "status": (
            "READY_FOR_EXPLICIT_REBUILD_AUTHORIZATION"
            if all_absent
            else "BLOCKED_EXISTING_DAILY_FAVORITE"
        ),
        "quote_date": daily_dir.name,
        "prior_manifest_sha256": checkpoint.get("manifest_sha256"),
        "prior_bundle_sha256": checkpoint.get("bundle_sha256"),
        "prior_checkpoint_status": checkpoint.get("status"),
        "prior_checkpoint_file_sha256": hashlib.sha256((daily_dir / CHECKPOINT_FILENAME).read_bytes()).hexdigest(),
        "prior_report_file_sha256": hashlib.sha256((daily_dir / REPORT_FILENAME).read_bytes()).hexdigest(),
        "prior_report_completed_at": report.get("completed_at"),
        "title_results": rows,
        "all_titles_absent": all_absent,
        "checkpoint_reset_count": 0,
        "wechat_mutation_count": 0,
        "chat_send_count": 0,
        "shijiu_request_count": 0,
    }


def _archive_and_reset_completed_checkpoint(
    daily_dir: Path,
    preflight: dict[str, Any],
    audit: dict[str, Any],
) -> dict[str, Any]:
    checkpoint, report = _validate_rebuildable_production_record(daily_dir)
    if audit.get("status") != "READY_FOR_EXPLICIT_REBUILD_AUTHORIZATION":
        raise WeChatDailyProductionError("safe rebuild title audit is not ready")
    if audit.get("all_titles_absent") is not True:
        raise WeChatDailyProductionError("one or more daily Favorites still exist")
    if len(audit.get("title_results") or []) != 2 or any(
        int(row.get("search_candidate_count") or 0) != 0
        for row in audit.get("title_results") or []
    ):
        raise WeChatDailyProductionError("one or more daily Favorites still exist")
    for name, key in ((CHECKPOINT_FILENAME, "prior_checkpoint_file_sha256"), (REPORT_FILENAME, "prior_report_file_sha256")):
        if hashlib.sha256((daily_dir / name).read_bytes()).hexdigest() != audit.get(key):
            raise WeChatDailyProductionError("checkpoint/report changed after absence audit")

    history_root = daily_dir / REBUILD_HISTORY_DIRECTORY
    history_root.mkdir(parents=True, exist_ok=True)
    rebuild_id = f"{datetime.now(ZoneInfo('Asia/Tokyo')).strftime('%Y%m%dT%H%M%S%f')}-{uuid.uuid4().hex[:8]}"
    temporary = history_root / f".{rebuild_id}.tmp"
    archive = history_root / rebuild_id
    temporary.mkdir()
    source_names = {CHECKPOINT_FILENAME, REPORT_FILENAME}
    # Include historical validation/audit records that an interrupted stage
    # may have written before adding an evidence_file pointer to checkpoint.
    source_names.update(path.name for path in daily_dir.glob("wechat_*validation*.json"))
    source_names.update(path.name for path in daily_dir.glob("wechat_*audit*.json"))
    for stage in STAGE_ORDER:
        evidence_file = str((checkpoint.get("stages") or {}).get(stage, {}).get("evidence_file") or "")
        if evidence_file:
            if Path(evidence_file).name != evidence_file or not (daily_dir / evidence_file).is_file():
                raise WeChatDailyProductionError("referenced evidence is missing or outside daily directory")
            source_names.add(evidence_file)
    for stage in STAGE_ORDER:
        recovery = (checkpoint.get("stages") or {}).get(stage, {}).get("authorized_recovery") or {}
        evidence_file = recovery.get("evidence_file")
        if evidence_file:
            if Path(evidence_file).name != evidence_file or not (daily_dir / evidence_file).is_file():
                raise WeChatDailyProductionError("recovery evidence is missing or outside daily directory")
            source_names.add(evidence_file)
    archived_files: list[dict[str, Any]] = []
    for name in sorted(source_names):
        source = daily_dir / name
        if source.is_symlink():
            raise WeChatDailyProductionError("archive source must not be a symlink")
        if not source.is_file():
            continue
        target = temporary / name
        shutil.copy2(source, target)
        if target.read_bytes() != source.read_bytes():
            raise WeChatDailyProductionError("archive copy verification failed")
        archived_files.append(
            {
                "name": name,
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "byte_count": source.stat().st_size,
            }
        )
    archive_manifest = {
        "schema_version": 1,
        "status": ("PREVIOUS_PASS_ARCHIVED_BEFORE_SAFE_REBUILD" if checkpoint["status"] == "PASS"
                   else "PREVIOUS_FROZEN_ARCHIVED_BEFORE_SAFE_REBUILD"),
        "prior_checkpoint_status": checkpoint["status"],
        "replacement_manifest_sha256": preflight["manifest_sha256"],
        "replacement_bundle_sha256": preflight["bundle_sha256"],
        "previous_bundle_matches_current": checkpoint["bundle_sha256"] == preflight["bundle_sha256"],
        "rebuild_id": rebuild_id,
        "archived_at": _now(),
        "quote_date": daily_dir.name,
        "prior_manifest_sha256": checkpoint.get("manifest_sha256"),
        "prior_bundle_sha256": checkpoint.get("bundle_sha256"),
        "prior_report_sha256": _canonical_sha256(report),
        "absence_audit": audit,
        "files": archived_files,
        "wechat_mutation_count": 0,
    }
    write_json(temporary / "archive_manifest.json", archive_manifest)
    temporary.replace(archive)

    replacement = _new_checkpoint(preflight)
    replacement["safe_rebuild"] = {
        "status": "AUTHORIZED_RESET_AFTER_BOTH_TITLES_ABSENT",
        "rebuild_id": rebuild_id,
        "archive_directory": str(archive.relative_to(daily_dir)),
        "prior_manifest_sha256": checkpoint.get("manifest_sha256"),
        "prior_bundle_sha256": checkpoint.get("bundle_sha256"),
        "absence_audit_sha256": _canonical_sha256(audit),
        "reset_at": _now(),
    }
    write_json(daily_dir / CHECKPOINT_FILENAME, replacement)
    write_json(
        daily_dir / REPORT_FILENAME,
        {
            "schema_version": 1,
            "status": "SAFE_REBUILD_IN_PROGRESS",
            "quote_date": preflight["quote_date"],
            "manifest_sha256": preflight["manifest_sha256"],
            "bundle_sha256": preflight["bundle_sha256"],
            "rebuild_id": rebuild_id,
            "automatic_mutation_retry_count": 0,
            "wechat_mutation_count": 0,
            "chat_send_count": 0,
            "shijiu_request_count": 0,
        },
    )
    return replacement


def _validate_checkpoint(checkpoint: dict[str, Any], preflight: dict[str, Any]) -> None:
    for key in ("quote_date", "manifest_sha256", "bundle_sha256"):
        if checkpoint.get(key) != preflight.get(key):
            raise WeChatDailyProductionError(f"checkpoint {key} does not match current bundle")
    for stage in STAGE_ORDER:
        row = (checkpoint.get("stages") or {}).get(stage) or {}
        if row.get("payload_sha256") != preflight["payload_sha256"][stage]:
            raise WeChatDailyProductionError(f"checkpoint {stage} payload hash mismatch")
        if row.get("status") not in {"PENDING", "PASS"}:
            raise WeChatDailyProductionError(
                f"checkpoint is frozen at {stage}:{row.get('status')}; reconciliation required"
            )


def audit_frozen_pdf_recovery_readonly(
    daily_dir: Path,
    runtime_config: dict[str, Any],
    *,
    repository_root: Path,
    sink: MacWeChatFavoriteSink | None = None,
) -> dict[str, Any]:
    """Prove that the frozen run owns one repairable PDF note and no text note."""
    preflight = validate_daily_production_bundle(
        daily_dir,
        runtime_config,
        repository_root=repository_root,
        require_write_enabled=False,
    )
    daily_dir = daily_dir.resolve()
    checkpoint_path = daily_dir / CHECKPOINT_FILENAME
    if not checkpoint_path.is_file():
        raise WeChatDailyProductionError("PDF recovery requires a frozen checkpoint")
    checkpoint = _read_json(checkpoint_path)
    for key in ("quote_date", "manifest_sha256", "bundle_sha256"):
        if checkpoint.get(key) != preflight.get(key):
            raise WeChatDailyProductionError(
                f"PDF recovery checkpoint {key} does not match current bundle"
            )
    pdf_stage = (checkpoint.get("stages") or {}).get("pdf") or {}
    text_stage = (checkpoint.get("stages") or {}).get("text") or {}
    if checkpoint.get("status") != "FROZEN_RECONCILIATION_REQUIRED":
        raise WeChatDailyProductionError("PDF recovery requires frozen reconciliation status")
    prior_recovery = pdf_stage.get("authorized_recovery") or {}
    title_scoped_resume = (
        pdf_stage.get("status") == "FROZEN_AFTER_RECOVERY_MUTATION_ATTEMPT"
        and prior_recovery.get("error") == "expected one owned note window, got 2"
        and prior_recovery.get("status") == "FROZEN_AFTER_RECOVERY_MUTATION_ATTEMPT"
        and prior_recovery.get("mutation_attempt_count") == 1
        and not pdf_stage.get("payload_title_recovery_started_at")
    )
    native_reference_resume = (
        pdf_stage.get("status") == "FROZEN_AFTER_RECOVERY_MUTATION_ATTEMPT"
        and prior_recovery.get("error") == "native file reference URL cannot resolve to path"
        and prior_recovery.get("status") == "FROZEN_AFTER_RECOVERY_MUTATION_ATTEMPT"
        and prior_recovery.get("mutation_attempt_count") == 1
        and bool(pdf_stage.get("payload_title_recovery_started_at"))
        and not pdf_stage.get("native_reference_recovery_started_at")
        and runtime_config.get("coordinate_free_pdf_runtime_validation_status") == "PASS"
    )
    explicit_resume = title_scoped_resume or native_reference_resume
    if pdf_stage.get("status") != "FROZEN_AFTER_MUTATION_ATTEMPT" and not explicit_resume:
        raise WeChatDailyProductionError("PDF recovery requires one frozen PDF attempt")
    title_timeout = str(pdf_stage.get("error") or "").startswith(
        "post-readback note title did not stabilize; no picker opened: ")
    if not title_timeout and pdf_stage.get("error") not in {
        "attachment was not visible before save",
        "post-readback note title does not match verified body",
    }:
        raise WeChatDailyProductionError("PDF recovery error is not the canonical attachment failure")
    if int(pdf_stage.get("mutation_attempt_count") or 0) != 1:
        raise WeChatDailyProductionError("PDF recovery requires exactly one original PDF attempt")
    if text_stage.get("status") != "PENDING" or int(
        text_stage.get("mutation_attempt_count") or 0
    ) != 0:
        raise WeChatDailyProductionError("PDF recovery requires an untouched text stage")
    if int(checkpoint.get("favorite_create_count") or 0) != 0:
        raise WeChatDailyProductionError("PDF recovery checkpoint already counts a created Favorite")
    if prior_recovery.get("mutation_started_at") and not explicit_resume:
        raise WeChatDailyProductionError("PDF recovery has already been attempted")

    target = sink or MacWeChatFavoriteSink(runtime_config)
    titles = _daily_titles(daily_dir)
    audit = target.audit_titles_read_only(titles)
    rows = list(audit.get("results") or [])
    counts = [int(row.get("search_candidate_count") or 0) for row in rows]
    if len(rows) != 2 or counts != [1, 0]:
        raise WeChatDailyProductionError(
            "PDF recovery requires exactly one PDF title and zero text titles"
        )
    draft_proof = None
    if explicit_resume or title_timeout:
        draft_proof = target.audit_pdf_draft_read_only(preflight["payloads"]["pdf"])
        if (draft_proof.get("status") != "VERIFIED_PAYLOAD_DRAFT_WITHOUT_ATTACHMENT"
                or draft_proof.get("attachment_marker_count") != 0
                or not (draft_proof.get("comparison") or {}).get("normalized_hash_match")):
            raise WeChatDailyProductionError("title-scoped recovery draft proof failed")
    return {
        "schema_version": 1,
        "status": "READY_FOR_EXPLICIT_PDF_RECOVERY_AUTHORIZATION",
        "quote_date": daily_dir.name,
        "manifest_sha256": preflight["manifest_sha256"],
        "bundle_sha256": preflight["bundle_sha256"],
        "title_results": rows,
        "exact_title_counts": counts,
        "title_scoped_explicit_recovery": title_scoped_resume,
        "native_reference_explicit_recovery": native_reference_resume,
        "payload_draft_proof": draft_proof,
        "pdf_original_mutation_attempt_count": 1,
        "text_mutation_attempt_count": 0,
        "checkpoint_reset_count": 0,
        "wechat_mutation_count": 0,
        "chat_send_count": 0,
        "shijiu_request_count": 0,
    }


@locked_daily_output
def recover_frozen_pdf_and_complete_daily_favorites(
    daily_dir: Path,
    runtime_config: dict[str, Any],
    *,
    repository_root: Path,
    confirmation: str | None,
    sink: MacWeChatFavoriteSink | None = None,
) -> dict[str, Any]:
    """Recover one proven text-only PDF note, then create the pending text note.

    This path never creates a second PDF note.  It is available only for the
    exact frozen attachment failure produced by the current rebuild attempt,
    after a read-only proof of one PDF title and zero text titles.  The toolbar
    picker mutation is single-attempt and separately checkpointed.
    """

    preflight = validate_daily_production_bundle(
        daily_dir,
        runtime_config,
        repository_root=repository_root,
        require_write_enabled=True,
    )
    if confirmation != PRODUCTION_CONFIRMATION:
        raise WeChatDailyProductionError("exact production confirmation is missing")
    recovery_contract = runtime_config.get("pdf_attachment_recovery") or {}
    if (
        recovery_contract.get("strategy") != PDF_FILE_PICKER_RECOVERY_MODE
        or recovery_contract.get("requires_explicit_operator_authorization") is not True
        or recovery_contract.get("single_attempt_only") is not True
        or recovery_contract.get("automatic_retry_enabled") is not False
    ):
        raise WeChatDailyProductionError("PDF recovery runtime contract is not fail-closed")
    daily_dir = daily_dir.resolve()
    checkpoint_path = daily_dir / CHECKPOINT_FILENAME
    report_path = daily_dir / REPORT_FILENAME
    checkpoint = _read_json(checkpoint_path)
    pdf_stage = checkpoint["stages"]["pdf"]
    target = sink or MacWeChatFavoriteSink(runtime_config)
    audit = audit_frozen_pdf_recovery_readonly(
        daily_dir,
        runtime_config,
        repository_root=repository_root,
        sink=target,
    )
    recovery_state = {
        "status": "MUTATION_STARTED",
        "mutation_started_at": _now(),
        "mutation_attempt_count": 1,
        "title_audit": audit,
        "automatic_retry_count": 0,
    }
    if audit["title_scoped_explicit_recovery"] or audit["native_reference_explicit_recovery"]:
        # A new operator-authorized task, not replay of the consumed permit.
        # Preserve the failed attempt permanently and disallow another resume.
        pdf_stage.setdefault("recovery_history", []).append(pdf_stage["authorized_recovery"])
        marker = ("native_reference_recovery_started_at" if audit["native_reference_explicit_recovery"]
                  else "payload_title_recovery_started_at")
        pdf_stage[marker] = recovery_state["mutation_started_at"]
    pdf_stage["authorized_recovery"] = recovery_state
    checkpoint["updated_at"] = _now()
    write_json(checkpoint_path, checkpoint)
    try:
        evidence = target.recover_existing_pdf_attachment(
            preflight["payloads"]["pdf"],
            production=True,
            confirmation=confirmation,
            authorized_manifest_sha256=preflight["manifest_sha256"],
        )
        attachment_path = Path(preflight["payloads"]["pdf"]["attachments"][0])
        contract = validate_pdf_file_picker_recovery_evidence(
            evidence,
            attachment_path=attachment_path,
            expected_title=str(preflight["payloads"]["pdf"]["title"]),
        )
    except Exception as exc:
        recovery_state.update(
            {
                "status": "FROZEN_AFTER_RECOVERY_MUTATION_ATTEMPT",
                "failed_at": _now(),
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
        pdf_stage["status"] = "FROZEN_AFTER_RECOVERY_MUTATION_ATTEMPT"
        checkpoint["status"] = "FROZEN_RECONCILIATION_REQUIRED"
        checkpoint["updated_at"] = _now()
        write_json(checkpoint_path, checkpoint)
        write_json(
            report_path,
            {
                "schema_version": 1,
                "status": "FROZEN_RECONCILIATION_REQUIRED",
                "quote_date": preflight["quote_date"],
                "manifest_sha256": preflight["manifest_sha256"],
                "bundle_sha256": preflight["bundle_sha256"],
                "failed_stage": "pdf_file_picker_recovery",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "automatic_mutation_retry_count": 0,
                "chat_send_count": 0,
                "shijiu_request_count": 0,
            },
        )
        raise WeChatDailyProductionError(
            f"PDF toolbar recovery failed and was frozen without retry: {exc}"
        ) from exc

    evidence_file = "wechat_pdf_favorite_manual_file_picker_recovery_validation.json"
    write_json(daily_dir / evidence_file, evidence)
    recovery_state.update(
        {
            "status": "PASS",
            "completed_at": _now(),
            "evidence_file": evidence_file,
            "contract": contract,
        }
    )
    pdf_stage["status"] = "PASS"
    pdf_stage["completed_at"] = recovery_state["completed_at"]
    pdf_stage["evidence_file"] = evidence_file
    pdf_stage["final_attachment_method"] = PDF_FILE_PICKER_RECOVERY_MODE
    checkpoint["favorite_create_count"] = 1
    checkpoint["status"] = "IN_PROGRESS"
    checkpoint["updated_at"] = _now()
    write_json(checkpoint_path, checkpoint)
    write_json(
        report_path,
        {
            "schema_version": 1,
            "status": "PDF_RECOVERY_PASS_TEXT_PENDING",
            "quote_date": preflight["quote_date"],
            "manifest_sha256": preflight["manifest_sha256"],
            "bundle_sha256": preflight["bundle_sha256"],
            "pdf_attachment_recovery": contract,
            "favorite_create_count": 1,
            "automatic_mutation_retry_count": 0,
            "chat_send_count": 0,
            "shijiu_request_count": 0,
        },
    )
    result = save_daily_production_favorites(
        daily_dir,
        runtime_config,
        repository_root=repository_root,
        confirmation=confirmation,
        sink=target,
    )
    return {
        **result,
        "pdf_file_picker_recovery": contract,
        "recovered_existing_pdf_note_without_duplicate_create": True,
    }


@locked_daily_output
def save_daily_production_favorites(
    daily_dir: Path,
    runtime_config: dict[str, Any],
    *,
    repository_root: Path,
    confirmation: str | None,
    sink: MacWeChatFavoriteSink | None = None,
    rebuild_missing_daily: bool = False,
) -> dict[str, Any]:
    """Create exactly the two daily Favorites with resumable, no-retry stages."""

    preflight = validate_daily_production_bundle(
        daily_dir,
        runtime_config,
        repository_root=repository_root,
        require_write_enabled=True,
    )
    if confirmation != PRODUCTION_CONFIRMATION:
        raise WeChatDailyProductionError("exact production confirmation is missing")
    daily_dir = daily_dir.resolve()
    checkpoint_path = daily_dir / CHECKPOINT_FILENAME
    report_path = daily_dir / REPORT_FILENAME
    sink = sink or MacWeChatFavoriteSink(runtime_config)
    if rebuild_missing_daily and (
        runtime_config.get("production_authorization_mode") != "APP_ONE_TIME"
        or runtime_config.get("production_authorization_operation") != "REBUILD_MISSING_DAILY_TWO_FAVORITES"
    ):
        raise WeChatDailyProductionError("安全重建必须消费 App 专用单次授权，普通生产许可不能重置 checkpoint")
    if checkpoint_path.exists():
        checkpoint = _read_json(checkpoint_path)
        if rebuild_missing_daily:
            audit = audit_completed_daily_favorites_readonly(
                daily_dir,
                runtime_config,
                sink=sink,
                require_today=True,
            )
            if audit["status"] != "READY_FOR_EXPLICIT_REBUILD_AUTHORIZATION":
                raise WeChatDailyProductionError(
                    "safe rebuild blocked: one or more daily Favorite titles still exist"
                )
            current_preflight = validate_daily_production_bundle(
                daily_dir, runtime_config, repository_root=repository_root, require_write_enabled=True
            )
            if current_preflight["bundle_sha256"] != preflight["bundle_sha256"]:
                raise WeChatDailyProductionError("current bundle changed during read-only title audit; no reset")
            checkpoint = _archive_and_reset_completed_checkpoint(
                daily_dir, preflight, audit
            )
        else:
            _validate_checkpoint(checkpoint, preflight)
        if checkpoint.get("status") == "PASS":
            report = _read_json(report_path)
            if report.get("status") != "PASS" or report.get("bundle_sha256") != preflight["bundle_sha256"]:
                raise WeChatDailyProductionError("completed checkpoint/report mismatch")
            return {**report, "idempotent_replay": True}
    else:
        if rebuild_missing_daily:
            raise WeChatDailyProductionError(
                "safe rebuild requires an existing completed production checkpoint"
            )
        checkpoint = _new_checkpoint(preflight)
        write_json(checkpoint_path, checkpoint)

    evidence_paths: dict[str, str] = {}
    for stage in STAGE_ORDER:
        stage_state = checkpoint["stages"][stage]
        if stage_state["status"] == "PASS":
            evidence_paths[stage] = stage_state["evidence_file"]
            continue
        if any(
            checkpoint["stages"][earlier]["status"] != "PASS"
            for earlier in STAGE_ORDER[: STAGE_ORDER.index(stage)]
        ):
            raise WeChatDailyProductionError(f"cannot start {stage} before earlier stage PASS")
        stage_state["status"] = "MUTATION_STARTED"
        stage_state["mutation_started_at"] = _now()
        stage_state["mutation_attempt_count"] += 1
        checkpoint["status"] = "IN_PROGRESS"
        checkpoint["updated_at"] = _now()
        write_json(checkpoint_path, checkpoint)
        try:
            evidence = sink.save(
                preflight["payloads"][stage],
                production=True,
                confirmation=confirmation,
                authorized_manifest_sha256=preflight["manifest_sha256"],
            )
        except Exception as exc:
            stage_state["status"] = "FROZEN_AFTER_MUTATION_ATTEMPT"
            stage_state["error_type"] = type(exc).__name__
            stage_state["error"] = str(exc)
            checkpoint["status"] = "FROZEN_RECONCILIATION_REQUIRED"
            checkpoint["updated_at"] = _now()
            write_json(checkpoint_path, checkpoint)
            write_json(
                report_path,
                {
                    "schema_version": 1,
                    "status": "FROZEN_RECONCILIATION_REQUIRED",
                    "quote_date": preflight["quote_date"],
                    "manifest_sha256": preflight["manifest_sha256"],
                    "bundle_sha256": preflight["bundle_sha256"],
                    "failed_stage": stage,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "automatic_mutation_retry_count": 0,
                    "chat_send_count": 0,
                    "shijiu_request_count": 0,
                },
            )
            raise WeChatDailyProductionError(
                f"{stage} mutation/readback failed; batch frozen without retry: {exc}"
            ) from exc
        evidence_file = f"wechat_{stage}_favorite_production_validation.json"
        write_json(daily_dir / evidence_file, evidence)
        stage_state["status"] = "PASS"
        stage_state["completed_at"] = _now()
        stage_state["evidence_file"] = evidence_file
        stage_state["save_status"] = evidence.get("save_status", "SAVED_REOPEN_VERIFIED")
        stage_state["cleanup_status"] = (evidence.get("verification_close") or {}).get("status")
        checkpoint["favorite_create_count"] += 1
        checkpoint["updated_at"] = _now()
        write_json(checkpoint_path, checkpoint)
        evidence_paths[stage] = evidence_file

    checkpoint["status"] = "PASS"
    checkpoint["completed_at"] = _now()
    checkpoint["updated_at"] = _now()
    write_json(checkpoint_path, checkpoint)
    report = {
        "schema_version": 1,
        "status": "PASS",
        "production_readiness": "DAILY_EXACTLY_TWO_FAVORITES_SAVED_AND_REOPEN_VERIFIED",
        "quote_date": preflight["quote_date"],
        "manifest_sha256": preflight["manifest_sha256"],
        "bundle_sha256": preflight["bundle_sha256"],
        "favorite_create_count": checkpoint["favorite_create_count"],
        "favorite_order": list(STAGE_ORDER),
        "evidence_files": evidence_paths,
        "cleanup_warnings": [stage for stage in STAGE_ORDER
                             if checkpoint["stages"][stage].get("cleanup_status")
                             == "CLEANUP_FAILED_AFTER_VERIFIED_SAVE"],
        "automatic_mutation_retry_count": 0,
        "existing_favorite_mutation_count": 0,
        "safe_rebuild": checkpoint.get("safe_rebuild"),
        "chat_send_count": 0,
        "shijiu_request_count": 0,
        "completed_at": _now(),
    }
    write_json(report_path, report)
    return report
