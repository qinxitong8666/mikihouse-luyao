from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .daily_quote import sha256_json
from .wechat_favorite_runtime import (
    MacWeChatFavoriteSink,
    PRODUCTION_CONFIRMATION,
    text_fingerprint,
    write_json,
)


CHECKPOINT_FILENAME = "wechat_daily_production_checkpoint.json"
REPORT_FILENAME = "wechat_daily_production_report.json"
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


def save_daily_production_favorites(
    daily_dir: Path,
    runtime_config: dict[str, Any],
    *,
    repository_root: Path,
    confirmation: str | None,
    sink: MacWeChatFavoriteSink | None = None,
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
    if checkpoint_path.exists():
        checkpoint = _read_json(checkpoint_path)
        _validate_checkpoint(checkpoint, preflight)
        if checkpoint.get("status") == "PASS":
            report = _read_json(report_path)
            if report.get("status") != "PASS" or report.get("bundle_sha256") != preflight["bundle_sha256"]:
                raise WeChatDailyProductionError("completed checkpoint/report mismatch")
            return {**report, "idempotent_replay": True}
    else:
        checkpoint = _new_checkpoint(preflight)
        write_json(checkpoint_path, checkpoint)

    sink = sink or MacWeChatFavoriteSink(runtime_config)
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
        "automatic_mutation_retry_count": 0,
        "existing_favorite_mutation_count": 0,
        "chat_send_count": 0,
        "shijiu_request_count": 0,
        "completed_at": _now(),
    }
    write_json(report_path, report)
    return report
