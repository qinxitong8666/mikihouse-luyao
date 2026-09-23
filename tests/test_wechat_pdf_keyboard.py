from unittest.mock import Mock

import pytest

from mikihouse_luyao import wechat_pdf_keyboard as picker
from mikihouse_luyao import wechat_favorite_runtime as runtime


BODY = "MIKIHOUSE_TEST_2026-09-24_KEYBOARD_END\n正文首行\n正文末尾 END_20260924\n"
NOTE = runtime.WindowIdentity(1, "MIKIHOUSE_TEST_2026-", "AXWindow")


@pytest.fixture(autouse=True)
def verified_target(monkeypatch):
    monkeypatch.setattr(runtime, "select_target_process", lambda: {"pid": 123})


def test_script_has_no_coordinates_global_tree_or_repeated_action():
    script = picker.keyboard_picker_script(123, NOTE)
    assert "click at" not in script
    assert "entire contents" not in script
    assert "sheet 1 of targetNote" in script
    assert "AXIdentifier" in script and "open-panel" in script
    assert script.count('keystroke "o"') == 1
    assert script.index("key code 124") < script.index('keystroke "o"')
    assert script.index('keystroke "o"') < script.index("repeat 12 times")
    assert "NOTE_NOT_FRONTMOST" in script and "NOTE_NOT_UNIQUE" in script


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
        "returncode": 0, "stdout": "NOTE_OWNED_OPEN_PANEL_CONFIRMED", "stderr": "",
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
