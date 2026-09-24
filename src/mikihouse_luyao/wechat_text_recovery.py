"""Explicit, one-shot recovery of the frozen 2026-09-24 text prefix.

Not a generic resume switch: no CREATE, replacement, PDF UI access or retries.
The ordinary App production gates remain unchanged.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from . import wechat_favorite_runtime as r
from .daily_quote_guard import locked_daily_output
from .wechat_daily_production import (
    CHECKPOINT_FILENAME, REPORT_FILENAME, _now, validate_daily_production_bundle,
)

CONFIRMATION = "CONFIRM_APPEND_ONLY_MIKIHOUSE_20260924_TEXT_CHUNKS_3_TO_13"
PREFIX_SHA = "21679a583bc625fd541b403ff0a39e181cec9c48e5360ccfd5fc6d66d3d49799"
PAYLOAD_SHA = "c1d39d766c3ad1198afafeac5c2e1ebec94f0d2549ec11dea6366253cae071c2"
TITLE = "MIKI HOUSE 9月24日报价｜文字版"


def eol_text(value: str) -> str:
    # ONLY transport newline encoding; never strip spaces or trailing lines.
    return value.replace("\r\n", "\n").replace("\r", "\n")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def prove_chunk_prefix(expected: str, actual: str, chunk_chars: int = 5500) -> dict:
    chunks = r.split_text_chunks(expected, chunk_chars)
    canonical = eol_text(actual)
    boundaries = []
    length = 0
    for chunk in chunks:
        length += len(chunk)
        boundaries.append(length)
    matches = [i for i in range(1, len(chunks)) if "".join(chunks[:i]) == canonical]
    if len(matches) != 1:
        raise r.WeChatRuntimeError("not one exact EOL-only complete chunk boundary; no append")
    count = matches[0]
    return {"status": "EXACT_COMPLETE_CHUNK_PREFIX", "sent_chunk_count": count,
            "total_chunk_count": len(chunks), "remaining_chunk_count": len(chunks)-count,
            "prefix_character_count": len(canonical), "total_character_count": len(expected),
            "raw_exact_match": actual == "".join(chunks[:count]),
            "newline_transport_only": True, "prefix_sha256": digest(canonical),
            "raw_readback_sha256": digest(actual), "expected_sha256": digest(expected),
            "boundaries": boundaries, "remaining_character_count": len(expected)-len(canonical)}


def require_frozen_scope(checkpoint: dict, preflight: dict) -> None:
    if checkpoint.get("quote_date") != "2026-09-24":
        raise r.WeChatRuntimeError("recovery is limited to 2026-09-24")
    for key in ("quote_date", "manifest_sha256", "bundle_sha256"):
        if checkpoint.get(key) != preflight.get(key):
            raise r.WeChatRuntimeError("frozen bundle changed; no append")
    pdf, text = checkpoint["stages"]["pdf"], checkpoint["stages"]["text"]
    if (checkpoint.get("status") != "FROZEN_RECONCILIATION_REQUIRED" or pdf.get("status") != "PASS"
            or text.get("status") != "FROZEN_AFTER_MUTATION_ATTEMPT"
            or text.get("payload_sha256") != PAYLOAD_SHA
            or preflight["payload_sha256"]["text"] != PAYLOAD_SHA
            or text.get("mutation_attempt_count") != 1
            or "WINDOW_NOT_UNIQUE" not in text.get("error", "")
            or '笔记' not in text.get("error", "")
            or text.get("append_only_recovery") is not None):
        raise r.WeChatRuntimeError("not the authorized untouched partial-text failure; no replay")


@locked_daily_output
def recover_text_once(daily_dir: Path, runtime_config: dict, *, repository_root: Path,
                      confirmation: str = "", audit_only: bool = True) -> dict:
    if not audit_only and confirmation != CONFIRMATION:
        raise r.WeChatRuntimeError("explicit append-only task confirmation required")
    preflight = validate_daily_production_bundle(daily_dir, runtime_config,
                    repository_root=repository_root, require_write_enabled=False)
    cp_path = daily_dir / CHECKPOINT_FILENAME
    checkpoint = json.loads(cp_path.read_text())
    require_frozen_scope(checkpoint, preflight)
    payload = preflight["payloads"]["text"]
    if payload["title"] != TITLE or payload.get("attachments"):
        raise r.WeChatRuntimeError("text title/attachments mismatch")
    expected = (TITLE + "\n" + payload["body"]).rstrip() + "\n"
    chunks = r.split_text_chunks(expected, 5500)
    process = r.select_target_process(); pid, bundle = process["pid"], process["bundle_id"]
    search = r.search_saved_note_candidates(pid, bundle, TITLE)
    if search["search_candidate_count"] != 1:
        raise r.WeChatRuntimeError("text exact title must have exactly one saved candidate")
    candidates = r.payload_note_windows(pid, TITLE)
    note = (r.require_payload_note_window(pid, TITLE) if candidates
            else r.search_and_open_saved_note(pid, bundle, TITLE)[0])
    actual, readback = r.read_note_text(pid, bundle, note, max_attempts=1)
    proof = prove_chunk_prefix(expected, actual)
    if (proof["sent_chunk_count"] != 2 or proof["total_chunk_count"] != 13
            or proof["prefix_character_count"] != 10952 or proof["total_character_count"] != 70650
            or proof["prefix_sha256"] != PREFIX_SHA):
        raise r.WeChatRuntimeError("not the frozen two-chunk prefix; no append")
    audit = {"status": "PROVEN_APPEND_ONLY_SCOPE", "proof": proof,
             "saved_title_count": 1, "readback": readback, "pdf_ui_access_count": 0,
             "no_new_note": True, "no_replace": True}
    if audit_only:
        return audit
    # Protected before-state is saved before writing either checkpoint or note.
    before = daily_dir / "wechat_text_append_before"
    if before.exists():
        raise r.WeChatRuntimeError("recovery before-state already exists; no replay")
    r.write_json(before / CHECKPOINT_FILENAME, checkpoint)
    r.write_json(before / REPORT_FILENAME, json.loads((daily_dir / REPORT_FILENAME).read_text()))
    pdf_state = copy.deepcopy(checkpoint["stages"]["pdf"])
    state = {"status": "STARTED", "started_at": _now(), "authorization": CONFIRMATION,
             "audit": audit, "chunk_ledger": [], "automatic_retry_count": 0}
    checkpoint["stages"]["text"]["append_only_recovery"] = state
    def persist():
        if checkpoint["stages"]["pdf"] != pdf_state:
            raise r.WeChatRuntimeError("PDF checkpoint was changed")
        checkpoint["updated_at"] = _now()
        r.write_json(cp_path, checkpoint)
    def progress(event):
        state["chunk_ledger"].append({**event, "chunk_index": event["resume_chunk_offset"]+2, "at": _now()})
        persist()
    persist()
    try:
        appended = r.append_note_chunks(pid, bundle, note, expected_prefix="".join(chunks[:2]),
                                       remaining_chunks=chunks[2:], strict_eol=True, progress=progress)
        closed = r.close_and_save_note(pid, bundle, note, native=True)
        reopened, reopen = r.search_and_open_saved_note(pid, bundle, TITLE)
        final, final_read, _ = r.read_note_text_until_match(pid, bundle, reopened, expected)
        if eol_text(final) != expected:
            raise r.WeChatRuntimeError("reopened full EOL-only hash mismatch; freeze")
        verified_close = r.close_and_save_note(pid, bundle, reopened, native=True)
        final_search = r.search_saved_note_candidates(pid, bundle, TITLE)
        if final_search["search_candidate_count"] != 1:
            raise r.WeChatRuntimeError("final text title is not unique")
        evidence = {"status": "PASS", "mode": "APPEND_ONLY_EXISTING_TEXT", "audit": audit,
                    "append": appended, "close": closed, "reopen": reopen,
                    "final_readback": final_read, "expected_sha256": digest(expected),
                    "actual_eol_sha256": digest(eol_text(final)), "full_hash_match": True,
                    "verified_close": verified_close, "final_title_count": 1,
                    "pdf_ui_access_count": 0, "new_note_count": 0,
                    "resent_chunk_count": 0, "automatic_retry_count": 0}
        evidence_file = "wechat_text_append_recovery_validation.json"
        r.write_json(daily_dir / evidence_file, evidence)
        state.update(status="PASS", completed_at=_now())
        checkpoint["stages"]["text"].update(status="PASS", completed_at=_now(), evidence_file=evidence_file)
        checkpoint.update(status="PASS", completed_at=_now(), favorite_create_count=2)
        persist()
        report = {"schema_version": 1, "status": "PASS", "quote_date": "2026-09-24",
                  "production_readiness": "DAILY_EXACTLY_TWO_FAVORITES_SAVED_AND_REOPEN_VERIFIED",
                  "manifest_sha256": preflight["manifest_sha256"], "bundle_sha256": preflight["bundle_sha256"],
                  "favorite_create_count": 2, "current_task_new_note_count": 0, "favorite_order": ["pdf", "text"],
                  "evidence_files": {"pdf": pdf_state["evidence_file"], "text": evidence_file},
                  "pdf_evidence_basis": "PRIOR_PASS_UNCHANGED_NOT_REOPENED_THIS_TASK",
                  "pdf_ui_access_count": 0, "automatic_mutation_retry_count": 0,
                  "chat_send_count": 0, "shijiu_request_count": 0, "completed_at": _now()}
        r.write_json(daily_dir / REPORT_FILENAME, report)
        return report
    except Exception as exc:
        state.update(status="FROZEN_NO_RETRY", error=str(exc), failed_at=_now())
        checkpoint.update(status="FROZEN_RECONCILIATION_REQUIRED")
        persist()
        r.write_json(daily_dir / REPORT_FILENAME, {"status": "FROZEN_RECONCILIATION_REQUIRED",
                     "failed_stage": "text_append_recovery", "error": str(exc),
                     "pdf_status": "PASS_UNCHANGED", "automatic_mutation_retry_count": 0})
        raise
