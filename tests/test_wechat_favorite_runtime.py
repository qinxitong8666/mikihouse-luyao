from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import mikihouse_luyao.wechat_favorite_runtime as runtime

from mikihouse_luyao.wechat_favorite_runtime import (
    PRODUCTION_CONFIRMATION,
    WeChatRuntimeError,
    compare_text_readback,
    normalize_wechat_text,
    remove_attachment_placeholders,
    split_text_chunks,
    summarize_runtime_readiness,
    text_fingerprint,
    validate_runtime_write_gate,
    WindowIdentity,
)


def test_text_fingerprint_and_readback_detect_truncation() -> None:
    expected = "MIKIHOUSE_TEST\n赤 13/13.5\uff5c719元\n"
    exact = compare_text_readback(expected, expected)
    assert exact["status"] == "EXACT_READBACK_MATCH"
    assert exact["exact_hash_match"] is True
    truncated = compare_text_readback(expected, expected[:-3])
    assert truncated["status"] == "READBACK_MISMATCH"
    assert truncated["truncated"] is True
    assert text_fingerprint(expected)["tail_sha256"] != truncated["actual"]["tail_sha256"]


def test_normalized_readback_allows_only_known_whitespace_normalization() -> None:
    assert normalize_wechat_text("A\r\nB\u00a0 \n") == "A\nB"
    result = compare_text_readback("A\r\nB\n", "A\nB")
    assert result["normalized_hash_match"] is True
    assert result["exact_hash_match"] is False


def test_pdf_readback_removes_only_exact_trailing_file_placeholder() -> None:
    actual = "title\rbody\r[文件]\r"
    assert remove_attachment_placeholders(actual, 1) == "title\nbody\n"
    with pytest.raises(WeChatRuntimeError, match="placeholder"):
        remove_attachment_placeholders("title\nbody\n", 1)


def test_chunk_split_is_lossless_and_prefers_line_boundaries() -> None:
    text = "\n".join(f"{index:04d}-line" for index in range(1000)) + "\n"
    chunks = split_text_chunks(text, max_chars=511)
    assert "".join(chunks) == text
    assert len(chunks) > 1
    assert all(len(chunk) <= 511 for chunk in chunks)
    boundary = "a" * 510 + "\n" + "b" * 510
    boundary_chunks = split_text_chunks(boundary, max_chars=511)
    assert "".join(boundary_chunks) == boundary
    assert all(len(chunk) <= 511 for chunk in boundary_chunks)


def test_note_window_filter_excludes_software_update(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "get_windows", lambda _pid: [
        WindowIdentity(1, "软件更新", "AXWindow"),
        WindowIdentity(2, "MIKIHOUSE_TEST_2026-", "AXWindow"),
        WindowIdentity(3, "WeChat", "AXWindow"),
    ])
    assert runtime._note_windows(123) == [
        WindowIdentity(2, "MIKIHOUSE_TEST_2026-", "AXWindow")
    ]


def test_readback_waits_for_delayed_clipboard_without_mutating_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"clipboard": "ORIGINAL", "polls": 0}
    expected = "MIKIHOUSE_TEST\n" + ("商品行\n" * 20000)

    def fake_clipboard_text() -> str:
        if str(state["clipboard"]).startswith("MIKIHOUSE_CLIPBOARD_SENTINEL_"):
            state["polls"] += 1
            if state["polls"] >= 3:
                return expected
        return str(state["clipboard"])

    monkeypatch.setattr(runtime, "_clipboard_text", fake_clipboard_text)
    monkeypatch.setattr(runtime, "_set_clipboard_text", lambda value: state.__setitem__("clipboard", value))
    monkeypatch.setattr(runtime, "_raise_note", lambda *_args: None)
    scripts: list[str] = []

    def fake_osascript(script: str) -> dict[str, object]:
        scripts.append(script)
        return {"returncode": 0, "stdout": "COPY_SENT", "stderr": ""}

    monkeypatch.setattr(runtime, "_osascript", fake_osascript)
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)
    actual, evidence = runtime.read_note_text(
        123,
        "com.tencent.xinWeChot2",
        WindowIdentity(1, "MIKIHOUSE_TEST_2026-", "AXWindow"),
    )
    assert actual == expected
    assert evidence["attempt_count"] == 1
    assert evidence["fingerprint"]["raw_character_count"] == len(expected)
    assert "delay 1.500" in scripts[0]
    assert state["clipboard"] == "ORIGINAL"


def test_runtime_test_gate_requires_explicit_test_title() -> None:
    config = {"runtime_validation_status": "PENDING", "production_save_enabled": False}
    with pytest.raises(WeChatRuntimeError, match="MIKIHOUSE_TEST"):
        validate_runtime_write_gate({"title": "ordinary"}, config, production=False)
    evidence = validate_runtime_write_gate(
        {"title": "MIKIHOUSE_TEST_2026-09-22", "attachments": []},
        config,
        production=False,
    )
    assert evidence["mode"] == "DISPOSABLE_TEST"
    assert evidence["chat_send_allowed"] is False
    assert evidence["automatic_delete_allowed"] is False


def test_production_gate_is_double_locked_and_manifest_bound() -> None:
    payload = {"title": "MIKI HOUSE 9月22日报价｜文字版", "source_manifest_sha256": "a" * 64}
    disabled = {
        "runtime_validation_status": "PASS",
        "production_save_enabled": False,
        "validated_manifest_sha256": "a" * 64,
    }
    with pytest.raises(WeChatRuntimeError, match="disabled"):
        validate_runtime_write_gate(
            payload, disabled, production=True, confirmation=PRODUCTION_CONFIRMATION
        )
    enabled = {**disabled, "production_save_enabled": True}
    with pytest.raises(WeChatRuntimeError, match="confirmation"):
        validate_runtime_write_gate(payload, enabled, production=True)
    evidence = validate_runtime_write_gate(
        payload, enabled, production=True, confirmation=PRODUCTION_CONFIRMATION
    )
    assert evidence["mode"] == "PRODUCTION"


def test_production_gate_rejects_stale_manifest() -> None:
    config = {
        "runtime_validation_status": "PASS",
        "production_save_enabled": True,
        "validated_manifest_sha256": "a" * 64,
    }
    with pytest.raises(WeChatRuntimeError, match="not validated"):
        validate_runtime_write_gate(
            {"title": "MIKI", "source_manifest_sha256": "b" * 64},
            config,
            production=True,
            confirmation=PRODUCTION_CONFIRMATION,
        )


def test_production_gate_accepts_only_explicit_fresh_manifest_binding() -> None:
    current = "b" * 64
    config = {
        "runtime_validation_status": "PASS",
        "production_save_enabled": True,
        "validated_manifest_sha256": "a" * 64,
        "allow_fresh_daily_manifest_binding": True,
    }
    evidence = validate_runtime_write_gate(
        {"title": "MIKI", "source_manifest_sha256": current},
        config,
        production=True,
        confirmation=PRODUCTION_CONFIRMATION,
        authorized_manifest_sha256=current,
    )
    assert evidence["manifest_binding_mode"] == "FRESH_DAILY_PRODUCTION_BUNDLE"
    with pytest.raises(WeChatRuntimeError, match="not validated"):
        validate_runtime_write_gate(
            {"title": "MIKI", "source_manifest_sha256": current},
            config,
            production=True,
            confirmation=PRODUCTION_CONFIRMATION,
            authorized_manifest_sha256="c" * 64,
        )


def test_production_title_collision_blocks_before_note_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = "b" * 64
    config = {
        "runtime_validation_status": "PASS",
        "production_save_enabled": True,
        "validated_manifest_sha256": "a" * 64,
        "allow_fresh_daily_manifest_binding": True,
    }
    monkeypatch.setattr(runtime, "select_target_process", lambda: {
        "pid": 123,
        "bundle_id": "com.tencent.xinWeChot2",
        "app_path": "/Applications/微信2.app",
        "version": "test",
    })
    monkeypatch.setattr(runtime, "search_saved_note_candidates", lambda *_args: {
        "search_candidate_count": 1,
        "candidate_lines": ["AXStaticText|||MIKI HOUSE 9月23日报价｜PDF版"],
    })
    created = {"count": 0}

    def unexpected_create(*_args: object) -> object:
        created["count"] += 1
        raise AssertionError("create_new_note must not run after a title collision")

    monkeypatch.setattr(runtime, "create_new_note", unexpected_create)
    sink = runtime.MacWeChatFavoriteSink(config)
    with pytest.raises(WeChatRuntimeError, match="refusing duplicate create"):
        sink.save(
            {
                "title": "MIKI HOUSE 9月23日报价｜PDF版",
                "body": "body",
                "attachments": [],
                "source_manifest_sha256": manifest,
            },
            production=True,
            confirmation=PRODUCTION_CONFIRMATION,
            authorized_manifest_sha256=manifest,
        )
    assert created["count"] == 0


def test_two_title_audit_is_read_only_and_requires_both_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "select_target_process", lambda: {
        "pid": 123,
        "bundle_id": "com.tencent.xinWeChot2",
        "app_path": "/Applications/微信2.app",
        "version": "test",
    })
    counts = iter([0, 1])
    monkeypatch.setattr(
        runtime,
        "search_saved_note_candidates",
        lambda _pid, _bundle, title: {
            "status": "SAVED_NOTE_CANDIDATES_READ_ONLY",
            "search_candidate_count": next(counts),
            "search_tree_sha256": "a" * 64,
            "search_marker_sha256": hashlib.sha256(title.encode()).hexdigest(),
        },
    )
    monkeypatch.setattr(
        runtime,
        "create_new_note",
        lambda *_args: (_ for _ in ()).throw(AssertionError("read-only audit must not create")),
    )
    result = runtime.MacWeChatFavoriteSink({}).audit_titles_read_only(
        ["MIKI HOUSE 9月23日报价｜PDF版", "MIKI HOUSE 9月23日报价｜文字版"]
    )
    assert [row["search_candidate_count"] for row in result["results"]] == [0, 1]
    assert result["all_titles_absent"] is False
    assert result["wechat_mutation_count"] == 0


def test_saved_note_candidate_parser_ignores_search_heading_false_duplicate() -> None:
    marker = "MIKI HOUSE 9月23日报价｜PDF版"
    tree = "\n".join([
        f'AXStaticText|||“{marker}”的搜索结果||||||',
        f'AXList|||{marker}||||||',
        f'AXTextField|||搜索|||Search|||{marker}',
        f'AXTextArea|||搜索||||||{marker}',
        f'AXStaticText|||笔记{marker}正文摘要||||||',
    ])
    assert runtime._exact_saved_note_candidate_lines(tree, marker) == [
        f'AXStaticText|||笔记{marker}正文摘要||||||',
    ]


def test_saved_note_candidates_count_duplicate_cards_not_selected_list() -> None:
    marker = "MIKI HOUSE 9月23日报价｜PDF版"
    card = f"AXStaticText|||笔记{marker}正文||||||"
    tree = f"AXList|||{marker}||||||\n{card}\n{card}"
    assert len(runtime._exact_saved_note_candidate_lines(tree, marker)) == 2
    # A truncated or longer same-prefix title must not authorize a rebuild.
    assert runtime._exact_saved_note_candidate_lines(card.replace("正文", "副本正文"), marker)


def test_numbered_pdf_title_cannot_authorize_frozen_recovery() -> None:
    marker = "MIKI HOUSE 9月23日报价｜PDF版"
    # Observed on 2026-09-23: the list repeats the search query, but its one
    # saved note card and note window have a numbered title. Never strip it.
    tree = "\n".join([
        f"AXList|||{marker}||||||",
        f"AXStaticText|||笔记1.{marker}MIKI HOUSE 日本官网当日报价||||||",
        f"AXTextArea|||搜索||||||{marker}",
    ])
    with pytest.raises(WeChatRuntimeError, match="不能判定标题不存在"):
        runtime._exact_saved_note_candidate_lines(tree, marker)


def test_title_absence_requires_current_search_heading_and_explicit_no_results():
    marker = "MIKI HOUSE 9月23日报价｜PDF版"
    heading = f"AXStaticText|||“{marker} ”的搜索结果||||||"
    empty = "AXStaticText|||||||||无结果"
    runtime.validate_saved_note_search_completion(heading + "\n" + empty, marker, 0)
    split = f"AXStaticText|||missing value||||||“{marker}\nAXStaticText|||missing value|||||| \nAXStaticText|||missing value||||||”的搜索结果"
    runtime.validate_saved_note_search_completion(split + "\n" + empty, marker, 0)
    for tree in (heading, empty, "AXTextArea|||搜索||||||" + marker,
                 split.replace(marker, "旧标题") + "\n" + empty,
                 split.replace("AXStaticText|||missing value||||||”", "AXTextArea|||missing value||||||”") + "\n" + empty,
                 "AXStaticText|||旧标题的搜索结果||||||\n" + empty):
        with pytest.raises(WeChatRuntimeError):
            runtime.validate_saved_note_search_completion(tree, marker, 0)


@pytest.mark.parametrize("tree", [
    "|||missing value||||||",
    "AXWindow|||WeChat||||||\nchildren_error|||denied",
    "AXList|||title||||||",
])
def test_unreadable_favorites_never_prove_absence(tree: str) -> None:
    with pytest.raises(WeChatRuntimeError):
        runtime._exact_saved_note_candidate_lines(tree, "title")


def test_ax_collection_uses_explicit_system_events_attributes(monkeypatch) -> None:
    scripts = []
    def capture(script):
        scripts.append(script)
        return {"returncode": 0, "stdout": "AXWindow|||WeChat||||||", "stderr": ""}
    monkeypatch.setattr(runtime, "_osascript", capture)
    runtime.collect_window_ax_text(1, WindowIdentity(1, "WeChat", "AXWindow"))
    handler = scripts[0].split("on describeNode", 1)[1].split("end describeNode", 1)[0]
    assert 'tell application "System Events"' in handler
    for attribute in ("AXRole", "AXTitle", "AXDescription", "AXValue"):
        assert f'value of attribute "{attribute}"' in handler


def test_close_and_save_note_waits_for_delayed_window_disappearance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = WindowIdentity(1, "WeChat", "AXWindow")
    note = WindowIdentity(2, "MIKI HOUSE 9月23日报价｜文", "AXWindow")
    inventories = iter([
        [main, note],
        [main, note],
        [main, note],
        [main],
    ])
    monkeypatch.setattr(runtime, "get_windows", lambda _pid: next(inventories))
    monkeypatch.setattr(runtime, "_raise_note", lambda *_args: None)
    monkeypatch.setattr(runtime, "press_menu_item", lambda *_args: {})
    sleeps: list[float] = []
    monkeypatch.setattr(runtime.time, "sleep", sleeps.append)
    evidence = runtime.close_and_save_note(123, "com.tencent.xinWeChot2", note)
    assert evidence["status"] == "NOTE_CLOSED_AUTO_SAVE_EXPECTED"
    assert evidence["close_poll_attempt_count"] == 3
    assert sleeps == [0.5, 0.5]


def test_toolbar_picker_attachment_is_single_attempt_and_strongly_read_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = tmp_path / "daily.pdf"
    attachment.write_bytes(b"%PDF-test")
    note = WindowIdentity(2, "MIKI HOUSE 9月23日报价｜P", "AXWindow")
    expected = "MIKI HOUSE 9月23日报价｜PDF版\n正文\n"
    readbacks = iter(
        [
            (expected, {"copy_status": "COPY_READ"}),
            (expected + "[\u6587\u4ef6]\n", {"copy_status": "COPY_READ"}),
        ]
    )
    monkeypatch.setattr(runtime, "read_note_text", lambda *_args, **_kwargs: next(readbacks))
    monkeypatch.setattr(runtime, "_raise_note", lambda *_args: None)
    monkeypatch.setattr(runtime, "_clipboard_text", lambda: "original")
    clipboard_values: list[str] = []
    monkeypatch.setattr(runtime, "_set_clipboard_text", clipboard_values.append)
    monkeypatch.setattr(runtime, "get_windows", lambda _pid: [note])
    monkeypatch.setattr(runtime, "select_target_process", lambda: {"pid": 123})
    monkeypatch.setattr(runtime, "collect_window_ax_text", lambda *_args: attachment.name)
    from mikihouse_luyao import wechat_pdf_keyboard as keyboard
    from mikihouse_luyao import wechat_native_picker as native
    from unittest.mock import MagicMock
    panel = MagicMock()
    panel.__enter__.return_value = panel
    panel.owned_panel.return_value = None
    panel.open_exact_file_once.return_value = {"api_returncode": -25205, "dispatch_count": 1}
    monkeypatch.setattr(native, "NativePickerAX", lambda *_: panel)
    monkeypatch.setattr(native, "wait_for_owned_panel", lambda *a, **kw: {"status": "NOTE_OWNED_OPEN_PANEL_CONFIRMED"})
    monkeypatch.setattr(keyboard.time, "sleep", lambda _: None)
    osascript_calls: list[str] = []

    def fake_osascript(script: str) -> dict:
        osascript_calls.append(script)
        if "PICKER_KEYS_SENT_ONCE" in script:
            return {
                "returncode": 0,
                "stdout": "PICKER_KEYS_SENT_ONCE",
                "stderr": "",
            }
        return {
            "returncode": 0,
            "stdout": "KEY_SENT_ONCE",
            "stderr": "",
        }

    monkeypatch.setattr(runtime, "_osascript", fake_osascript)
    result = runtime.attach_file_with_toolbar_picker(
        123,
        "com.tencent.xinWeChot2",
        note,
        attachment,
        expected_text=expected,
    )
    assert result["status"] == "ATTACHMENT_VISIBLE"
    assert result["method"] == "TOOLBAR_FILE_PICKER_SINGLE_ATTEMPT"
    assert result["automatic_retry_count"] == 0
    assert len(osascript_calls) == 3
    assert clipboard_values == []
    panel.open_exact_file_once.assert_called_once_with(attachment.resolve())
    assert result["selection"] == "EXACT_CFURL_PATH_AXOPEN"
    assert all("click at" not in script for script in osascript_calls)


def test_runtime_readiness_requires_both_reopened_favorites_and_full_capacity() -> None:
    capacity = {
        "status": "PASS",
        "full_text_status": "PASS",
        "full_text_character_count": 104006,
        "maximum_verified_body_characters": 104006,
        "truncation_observed": False,
    }
    pdf = {
        "status": "PASS",
        "reopened_title_present": True,
        "reopened_body_present": True,
        "reopened_attachment_filename_present": True,
    }
    text = {
        "status": "PASS",
        "reopened_title_present": True,
        "full_body_hash_match": True,
        "truncation_observed": False,
    }
    report = summarize_runtime_readiness(capacity, pdf, text)
    assert report["status"] == "PRODUCTION_READY_TWO_FAVORITES"
    assert report["production_save_enabled"] is False
    blocked = summarize_runtime_readiness({**capacity, "maximum_verified_body_characters": 90000}, pdf, text)
    assert blocked["status"] == "BLOCKED"


def test_tracked_2026_09_22_runtime_evidence_is_production_ready_but_save_off() -> None:
    root = Path(__file__).resolve().parents[1] / "outputs" / "daily_quote" / "2026-09-22"
    capacity = json.loads((root / "wechat_runtime_capacity_audit.json").read_text())
    pdf = json.loads((root / "wechat_pdf_favorite_runtime_validation.json").read_text())
    text = json.loads((root / "wechat_text_favorite_runtime_validation.json").read_text())
    compact = json.loads((root / "wechat_compact_text_experiment.json").read_text())
    compact_runtime = json.loads((root / "wechat_compact_text_runtime_validation.json").read_text())
    readiness = json.loads((root / "wechat_runtime_readiness.json").read_text())

    diagnosis = json.loads((root / "wechat_runtime_readback_diagnosis.json").read_text())

    assert capacity["maximum_verified_body_characters"] == 70661
    assert capacity["maximum_completed_capacity_ladder_body_characters"] == 30000
    assert capacity["full_text_status"] == "NOT_TESTED_STOP_ON_FIRST_FAILURE"
    assert capacity["production_text_status"] == "PASS"
    assert capacity["truncation_observed"] is False
    assert capacity["compact_70661_test_status"] == "PASS_SAVED_CLOSED_UNIQUE_SEARCH_REOPENED_FULL_HASH"
    assert pdf["status"] == "PASS"
    assert pdf["attachment_filename"] == "MIKIHOUSE_2026-09-22_报价全集.pdf"
    assert pdf["final_sync_indicator_cleared"] is True
    assert text["status"] == "PASS"
    assert text["full_104006_status"] == "NOT_TESTED_STOP_ON_FIRST_FAILURE"
    assert text["production_body_character_count"] == 70661
    assert text["full_body_hash_match"] is True
    assert compact["original"]["unicode_character_count"] == 104006
    assert compact["lossless_compact"]["unicode_character_count"] == 70661
    assert diagnosis["observed"]["saved_reopened_body_normalized_character_count"] == 55544
    assert diagnosis["observed"]["source_prefix_exact"] is True
    assert diagnosis["observed"]["full_60000_body_present"] is False
    assert diagnosis["followup"]["status"] == "PASS"
    assert compact_runtime["status"] == "PASS"
    assert compact_runtime["full_body_hash_match"] is True
    assert compact_runtime["truncation_observed"] is False
    assert readiness["daily_exactly_two_favorites_production_ready"] is True
    assert readiness["status"] == "PRODUCTION_READY_TWO_FAVORITES"
    assert readiness["production_save_enabled"] is False
    assert readiness["chat_send_count"] == 0
    assert readiness["shijiu_request_count"] == 0
