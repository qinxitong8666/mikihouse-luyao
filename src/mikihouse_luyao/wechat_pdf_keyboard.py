"""Coordinate-free picker preparation; deliberately not wired into production.

Command+O has been observed opening WeChat2's native note file picker. This
module only prepares that picker: it never selects a file, uploads, saves,
creates a Favorite, or retries a keyboard action. Full attachment acceptance
is still required before replacing the production path.
"""
from __future__ import annotations

import json
from typing import Any

from . import wechat_favorite_runtime as runtime


def _literal(value: str) -> str:
    if not value or any(ord(char) < 32 for char in value):
        raise runtime.WeChatRuntimeError("invalid exact note window title")
    return json.dumps(value, ensure_ascii=False)


def keyboard_picker_script(pid: int, note: runtime.WindowIdentity) -> str:
    """Address only the exact note and its own sheet, not all process contents."""
    title = _literal(note.title)
    return f'''
    tell application "System Events"
        {runtime._process_selector(pid)}
        tell targetProc
            set matchingNotes to every window whose name is {title}
            if (count of matchingNotes) is not 1 then return "NOTE_NOT_UNIQUE"
            set targetNote to item 1 of matchingNotes
            if (name of window 1 as text) is not {title} then return "NOTE_NOT_FRONTMOST"
            if (count of sheets of targetNote) is not 0 then return "EXISTING_SHEET"
            -- read_note_text leaves the proven whole body selected. Right
            -- collapses that selection at its end before opening the picker.
            key code 124
            keystroke "o" using command down
            -- Bounded observation only: never resend Command+O.
            repeat 12 times
                if (count of sheets of targetNote) is 1 then
                    set filePanel to sheet 1 of targetNote
                    if (value of attribute "AXIdentifier" of filePanel as text) is "open-panel" then
                        return "NOTE_OWNED_OPEN_PANEL_CONFIRMED"
                    end if
                    return "UNEXPECTED_SHEET"
                end if
                if (count of sheets of targetNote) > 1 then return "AMBIGUOUS_SHEETS"
                delay 0.25
            end repeat
            return "OPEN_PANEL_NOT_CONFIRMED"
        end tell
    end tell
    '''


def prepare_test_pdf_picker(
    pid: int,
    bundle_id: str,
    note: runtime.WindowIdentity,
    *,
    expected_text: str,
) -> dict[str, Any]:
    """Test-only: prove body, collapse selection to end, open one native sheet.

    A successful return is NOT an attachment/save/reopen acceptance. Caller
    must inspect/cancel the sheet or separately authorize a single file select.
    Existing attachments and all formal titles fail before keyboard actions.
    """
    title = expected_text.splitlines()[0] if expected_text else ""
    if bundle_id != runtime.TARGET_BUNDLE_ID:
        raise runtime.WeChatRuntimeError("only WeChat2 is allowed")
    if not title.startswith(runtime.TEST_TITLE_PREFIX + "_"):
        raise runtime.WeChatRuntimeError("keyboard picker is test-only")
    if not note.title.startswith(runtime.TEST_TITLE_PREFIX):
        raise runtime.WeChatRuntimeError("exact test note window required")
    if note.role != runtime.NOTE_WINDOW_ROLE:
        raise runtime.WeChatRuntimeError("test note must be an AXWindow")
    _literal(note.title)
    if "[文件]" in expected_text:
        raise runtime.WeChatRuntimeError("test body must not contain an attachment")
    if runtime.select_target_process()["pid"] != pid:
        raise runtime.WeChatRuntimeError("PID is not the verified WeChat2 process")
    current, readback = runtime.read_note_text(
        pid, bundle_id, note, max_attempts=1
    )
    comparison = runtime.compare_text_readback(expected_text, current)
    if not comparison["normalized_hash_match"] or "[文件]" in current:
        raise runtime.WeChatRuntimeError("test body readback mismatch; no picker opened")
    result = runtime._osascript(keyboard_picker_script(pid, note))
    if result["returncode"] != 0 or result["stdout"] != "NOTE_OWNED_OPEN_PANEL_CONFIRMED":
        raise runtime.WeChatRuntimeError(
            "keyboard picker not confirmed; do not retry: " + str(result)
        )
    return {
        "status": "PICKER_PREPARED_NOT_ATTACHMENT_ACCEPTED",
        "method": "COMMAND_O_NOTE_OWNED_AXSHEET_TEST_ONLY",
        "selection_placement": "RIGHT_COLLAPSE_AFTER_EXACT_WHOLE_BODY_READBACK",
        "body_readback": readback,
        "body_comparison": comparison,
        "panel_identifier": "open-panel",
        "automatic_action_retry_count": 0,
        "attachment_upload_count": 0,
        "production_integration_enabled": False,
    }
