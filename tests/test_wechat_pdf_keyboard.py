from unittest.mock import Mock

import pytest

from mikihouse_luyao import wechat_pdf_keyboard as picker
from mikihouse_luyao import wechat_favorite_runtime as runtime


BODY = "MIKIHOUSE_TEST_2026-09-24_KEYBOARD_END\n正文首行\n正文末尾 END_20260924\n"
NOTE = runtime.WindowIdentity(1, "MIKIHOUSE_TEST_2026-", "AXWindow")


@pytest.fixture(autouse=True)
def verified_target(monkeypatch):
    monkeypatch.setattr(runtime, "select_target_process", lambda: {"pid": 123})
    from mikihouse_luyao import wechat_native_picker as native
    monkeypatch.setattr(native, "wait_for_owned_panel", lambda *a, **kw: {
        "status": "NOTE_OWNED_OPEN_PANEL_CONFIRMED", "read_only_polls": 1,
    })


def test_script_has_no_coordinates_global_tree_or_repeated_action():
    script = picker.keyboard_picker_script(123, NOTE)
    assert "click at" not in script
    assert "entire contents" not in script
    assert "sheets of targetNote" in script
    assert script.count('key code 31 using command down') == 1
    assert script.index("key code 124") < script.index('key code 31')
    assert "key code 125 using command down" in script
    assert "key code 124 using command down" in script
    assert "repeat" not in script
    assert "NOTE_NOT_FRONTMOST" in script and "NOTE_NOT_UNIQUE" in script


def test_title_wait_rebinds_without_actions(monkeypatch):
    titles = iter(["笔记", "MIKIHOUSE_TEST_2026", NOTE.title, NOTE.title])
    monkeypatch.setattr(runtime, "get_windows", lambda _: [runtime.WindowIdentity(1, next(titles), "AXWindow")])
    monkeypatch.setattr(picker.time, "sleep", lambda _: None)
    action = Mock()
    monkeypatch.setattr(runtime, "_osascript", action)
    note, proof = picker.wait_for_verified_note_title(123, BODY.splitlines()[0])
    assert note.title == NOTE.title
    assert proof["read_only_poll_count"] == 4
    assert proof["mutation_count"] == 0
    action.assert_not_called()


@pytest.mark.parametrize("title", ["笔记", "other note", "MIKI"])
def test_title_wait_timeout_never_opens_picker(tmp_path, monkeypatch, title):
    path = tmp_path / "daily.pdf"
    path.write_bytes(b"%PDF-test")
    monkeypatch.setattr(runtime, "get_windows", lambda _: [runtime.WindowIdentity(1, title, "AXWindow")])
    monkeypatch.setattr(runtime, "read_note_text", lambda *a, **k: (BODY, {}))
    sleep = Mock()
    monkeypatch.setattr(picker.time, "sleep", sleep)
    opening = Mock()
    monkeypatch.setattr(picker, "open_picker_once", opening)
    with pytest.raises(runtime.WeChatRuntimeError, match="did not stabilize"):
        picker.wait_for_verified_note_title(123, BODY.splitlines()[0])
    assert sleep.call_count <= 20
    opening.assert_not_called()


def test_title_wait_ambiguous_window_fails_immediately(monkeypatch):
    monkeypatch.setattr(runtime, "get_windows", lambda _: [NOTE, NOTE])
    sleep = Mock()
    monkeypatch.setattr(picker.time, "sleep", sleep)
    with pytest.raises(runtime.WeChatRuntimeError, match="got 2"):
        picker.wait_for_verified_note_title(123, BODY.splitlines()[0])
    sleep.assert_not_called()


def test_body_proof_required_before_picker_after_readonly_title_wait(tmp_path, monkeypatch):
    path = tmp_path / "daily.pdf"
    path.write_bytes(b"%PDF-test")
    monkeypatch.setattr(runtime, "get_windows", lambda _: [NOTE])
    monkeypatch.setattr(runtime, "read_note_text", lambda *a, **k: ("foreign body", {}))
    wait = Mock(return_value=(NOTE, {}))
    monkeypatch.setattr(picker, "wait_for_verified_note_title", wait)
    with pytest.raises(runtime.WeChatRuntimeError, match="body mismatch"):
        picker.attach_pdf_once(123, runtime.TARGET_BUNDLE_ID, path, expected_text=BODY)
    wait.assert_called_once()


@pytest.mark.parametrize("reason", ["body", "attachment", "formal", "foreign"])
def test_unsafe_input_never_opens_picker(monkeypatch, reason):
    action = Mock()
    monkeypatch.setattr(runtime, "_osascript", action)
    actual = BODY if reason != "body" else "different body"
    if reason == "attachment":
        actual += "[文件]"
    monkeypatch.setattr(runtime, "read_note_text", lambda *a, **kw: (actual, {}))
    with pytest.raises(runtime.WeChatRuntimeError):
        picker.prepare_test_pdf_picker(
            123, "other.bundle" if reason == "foreign" else runtime.TARGET_BUNDLE_ID,
            NOTE, expected_text="正式收藏\n" if reason == "formal" else BODY,
        )
    action.assert_not_called()


@pytest.mark.parametrize("url,expected", [
    ("file:///tmp/%E6%8A%A5%E4%BB%B7.pdf", True),
    ("file://localhost/tmp/%E6%8A%A5%E4%BB%B7.pdf", True),
    ("https://host/tmp/%E6%8A%A5%E4%BB%B7.pdf", False),
    ("file://foreign/tmp/%E6%8A%A5%E4%BB%B7.pdf", False),
    ("file:///tmp/other.pdf", False),
    ("file:///tmp/%E6%8A%A5%E4%BB%B7.pdf?x=1", False),
    ("file:///.file/id=1.123", False),
])
def test_exact_file_url_requires_resolved_path(url, expected):
    from pathlib import Path
    from mikihouse_luyao.wechat_native_picker import exact_file_url
    # /tmp may resolve to /private/tmp on Mac; use a non-symlink path.
    url = url.replace("/tmp/", "/private/tmp/")
    assert exact_file_url(url, Path("/private/tmp/报价.pdf")) is expected


@pytest.mark.parametrize("count", [0, 2])
def test_filename_index_never_accepts_missing_or_ambiguous_note(monkeypatch, count):
    monkeypatch.setattr(runtime, "search_saved_note_candidates", lambda *a, **kw: {
        "search_candidate_count": count,
    })
    with pytest.raises(runtime.WeChatRuntimeError):
        runtime.verify_saved_attachment_index(123, runtime.TARGET_BUNDLE_ID, "TEST", BODY, ["daily.pdf"])


def test_filename_in_body_cannot_fake_file_proof(monkeypatch):
    search = Mock()
    monkeypatch.setattr(runtime, "search_saved_note_candidates", search)
    with pytest.raises(runtime.WeChatRuntimeError):
        runtime.verify_saved_attachment_index(123, runtime.TARGET_BUNDLE_ID, "TEST", "daily.pdf", ["daily.pdf"])
    search.assert_not_called()


def test_filename_index_uses_filename_query_and_separate_task_title(monkeypatch):
    search = Mock(return_value={"search_candidate_count": 1, "search_tree_sha256": "evidence"})
    monkeypatch.setattr(runtime, "search_saved_note_candidates", search)
    proof = runtime.verify_saved_attachment_index(123, runtime.TARGET_BUNDLE_ID, "TEST", BODY, ["daily.pdf"])
    search.assert_called_once_with(123, runtime.TARGET_BUNDLE_ID, "daily.pdf", candidate_title="TEST")
    assert proof[0]["filename"] == "daily.pdf"


@pytest.mark.parametrize("failure", ["open", "directory", "file", "panel_still_open", "readback"])
def test_native_failure_never_retries_upload(tmp_path, monkeypatch, failure):
    from unittest.mock import MagicMock
    from mikihouse_luyao import wechat_native_picker as native
    path = tmp_path / "daily.pdf"
    path.write_bytes(b"%PDF-test")
    panel = MagicMock()
    panel.__enter__.return_value = panel
    panel.owned_panel.return_value = 123 if failure == "panel_still_open" else None
    panel.open_exact_file_once.return_value = {
        "status": "DISPATCH_REQUIRES_READBACK", "api_returncode": -25205,
        "dispatch_count": 1,
    }
    monkeypatch.setattr(native, "NativePickerAX", lambda *_: panel)
    monkeypatch.setattr(picker.time, "sleep", lambda _: None)
    monkeypatch.setattr(runtime, "get_windows", lambda _: [NOTE])
    readbacks = iter([(BODY, {}), ("broken", {})])
    monkeypatch.setattr(runtime, "read_note_text", lambda *a, **k: next(readbacks))
    outputs = ["PICKER_KEYS_SENT_ONCE", "KEY_SENT_ONCE", "KEY_SENT_ONCE"]
    if failure == "open": outputs[0] = "NOT_CONFIRMED"
    responses = iter(outputs)
    monkeypatch.setattr(runtime, "_osascript", lambda _: {"returncode": 0, "stdout": next(responses)})
    if failure == "directory": panel.set_directory.side_effect = runtime.WeChatRuntimeError("wrong sheet")
    if failure == "file": panel.open_exact_file_once.side_effect = runtime.WeChatRuntimeError("unknown transport")
    with pytest.raises(runtime.WeChatRuntimeError):
        picker.attach_pdf_once(123, runtime.TARGET_BUNDLE_ID, path, expected_text=BODY)
    assert panel.open_exact_file_once.call_count == (1 if failure in ("file", "panel_still_open", "readback") else 0)


def test_exact_file_native_ambiguous_fails_before_action():
    from pathlib import Path
    from mikihouse_luyao.wechat_native_picker import NativePickerAX
    ax = object.__new__(NativePickerAX)
    ax.owned_panel = lambda: 1
    ax.nodes = lambda _: [2, 3]
    ax.value = lambda *_: "file:///private/tmp/daily.pdf"
    ax.actions = lambda _: ["AXOpen"]
    with pytest.raises(runtime.WeChatRuntimeError):
        ax.exact_file(Path("/private/tmp/daily.pdf"))


@pytest.mark.parametrize("method", ["save", "recover_existing_pdf_attachment"])
def test_unaccepted_runner_blocks_production_before_all_ui(monkeypatch, method):
    target = Mock()
    monkeypatch.setattr(runtime, "select_target_process", target)
    sink = runtime.MacWeChatFavoriteSink({"production_save_enabled": True})
    kwargs = {"production": True, "confirmation": runtime.PRODUCTION_CONFIRMATION}
    if method == "recover_existing_pdf_attachment":
        kwargs["authorized_manifest_sha256"] = "a" * 64
    with pytest.raises(runtime.WeChatRuntimeError, match="尚未通过"):
        getattr(sink, method)({"title": "正式PDF", "attachments": ["daily.pdf"]}, **kwargs)
    target.assert_not_called()


def test_runtime_failure_report_retains_panel_result(tmp_path, monkeypatch):
    path = tmp_path / "daily.pdf"
    path.write_bytes(b"%PDF-test")
    monkeypatch.setattr(runtime, "get_windows", lambda _: [NOTE])
    monkeypatch.setattr(runtime, "read_note_text", lambda *a, **k: (BODY, {}))
    monkeypatch.setattr(runtime, "_osascript", lambda _: {"returncode": 0, "stdout": "NOTE_NOT_FRONTMOST"})
    with pytest.raises(runtime.WeChatRuntimeError, match="NOTE_NOT_FRONTMOST"):
        picker.attach_pdf_once(123, runtime.TARGET_BUNDLE_ID, path, expected_text=BODY)


@pytest.mark.parametrize("output,code", [
    ("OPEN_PANEL_NOT_CONFIRMED", 0), ("AMBIGUOUS_SHEETS", 0),
    ("EXISTING_SHEET", 0), ("NOTE_NOT_UNIQUE", 0), ("", 1),
])
def test_panel_failure_never_retries(monkeypatch, output, code):
    monkeypatch.setattr(runtime, "read_note_text", lambda *a, **kw: (BODY, {}))
    action = Mock(return_value={"returncode": code, "stdout": output, "stderr": ""})
    monkeypatch.setattr(runtime, "_osascript", action)
    with pytest.raises(runtime.WeChatRuntimeError):
        picker.prepare_test_pdf_picker(123, runtime.TARGET_BUNDLE_ID, NOTE, expected_text=BODY)
    assert action.call_count == 1


def test_preparation_is_not_production_or_attachment_acceptance(monkeypatch):
    monkeypatch.setattr(runtime, "read_note_text", lambda *a, **kw: (BODY, {}))
    monkeypatch.setattr(runtime, "_osascript", lambda script: {
        "returncode": 0, "stdout": "PICKER_KEYS_SENT_ONCE", "stderr": "",
    })
    result = picker.prepare_test_pdf_picker(123, runtime.TARGET_BUNDLE_ID, NOTE, expected_text=BODY)
    assert result["status"] == "PICKER_PREPARED_NOT_ATTACHMENT_ACCEPTED"
    assert result["attachment_upload_count"] == 0
    assert result["production_integration_enabled"] is False


def test_title_literal_rejects_control_characters():
    with pytest.raises(runtime.WeChatRuntimeError):
        picker.keyboard_picker_script(123, runtime.WindowIdentity(1, "bad\ntitle", "AXWindow"))


def test_foreign_pid_never_reads_note_or_opens_picker(monkeypatch):
    read = Mock()
    action = Mock()
    monkeypatch.setattr(runtime, "read_note_text", read)
    monkeypatch.setattr(runtime, "_osascript", action)
    with pytest.raises(runtime.WeChatRuntimeError):
        picker.prepare_test_pdf_picker(456, runtime.TARGET_BUNDLE_ID, NOTE, expected_text=BODY)
    read.assert_not_called()
    action.assert_not_called()


def test_live_acceptance_does_not_enable_production_or_bypass_authorization(monkeypatch):
    import hashlib
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "config/wechat_favorite_runtime.json").read_text())
    evidence = json.loads((root / config["coordinate_free_pdf_runtime_evidence_path"]).read_text())
    assert config["coordinate_free_pdf_runtime_validation_status"] == "PASS"
    assert config["production_save_enabled"] is False
    assert evidence["status"] == "PASS_FULL_SINK_UNINTERRUPTED"
    assert evidence["sink_invocation_count"] == 1
    assert evidence["full_sink_interrupted"] is False
    assert evidence["body_readback"]["exact_hash_match"] is True
    assert evidence["attachment"]["file_dispatch"]["dispatch_count"] == 1
    assert evidence["attachment_filename_readback"][0]["exact_query_candidate_count"] == 1
    assert evidence["safety"]["formal_favorite_mutations_during_test"] == 0
    # Keep the previous failure immutable; pin only the new accepted runtime.
    current = json.loads((root / config["coordinate_free_pdf_runtime_blocking_evidence_path"]).read_text())
    assert current["status"] == "BLOCKED_NATIVE_FILE_REFERENCE"
    assert current["production_goal_completed"] is False
    # Preserve the accepted PDF snapshot. The shared runtime's subsequent text
    # binding change is pinned by its own append/save/reopen runtime evidence.
    current_binding = json.loads((root / config['retained_ax_window_runtime_evidence_path']).read_text())
    for relative, expected_hash in evidence["source_sha256"].items():
        if relative in current_binding["source_sha256"]:
            expected_hash = current_binding["source_sha256"][relative]
        assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == expected_hash
    # Historical real PASS is not acceptance of the new retained-AX path.
    if current_binding['status'] != 'PASS_SAVED_REOPENED':
        assert config['retained_ax_window_runtime_validation_status'] == 'BLOCKED_RUNTIME_ACCEPTANCE'
    target = Mock()
    monkeypatch.setattr(runtime, "select_target_process", target)
    with pytest.raises(runtime.WeChatRuntimeError):
        runtime.MacWeChatFavoriteSink(config).save(
            {"title": "正式PDF", "attachments": ["daily.pdf"]},
            production=True, confirmation=runtime.PRODUCTION_CONFIRMATION,
        )
    target.assert_not_called()


def test_reopened_body_cannot_hide_leading_duplicate_or_missing_attachment():
    for value in ("[文件]\n" + BODY, BODY + "[文件]\n[文件]\n", BODY):
        try:
            comparable = runtime.remove_attachment_placeholders(value, 1)
        except runtime.WeChatRuntimeError:
            continue
        assert not runtime.compare_text_readback(BODY, comparable)["normalized_hash_match"]
