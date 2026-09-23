"""Coordinate-free, single-attempt native PDF picker, behind the Sink gates."""
from __future__ import annotations

import json
import hashlib
import time
from pathlib import Path
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
            if frontmost is not true then return "PROCESS_NOT_FRONTMOST"
            if (name of window 1 as text) is not {title} then return "NOTE_NOT_FRONTMOST"
            if (count of sheets of targetNote) is not 0 then return "EXISTING_SHEET"
            -- Right alone did NOT clear selection in the real WeChat editor.
            -- Document end, then line end was verified with a trailing PDF.
            key code 125 using command down
            delay 0.2
            key code 124 using command down
            delay 0.2
            -- Physical O, independent of the active keyboard/IME layout.
            key code 31 using command down
            return "PICKER_KEYS_SENT_ONCE"
        end tell
    end tell
    '''


def open_picker_once(pid: int, note: runtime.WindowIdentity) -> dict[str, Any]:
    from .wechat_native_picker import wait_for_owned_panel
    result = runtime._osascript(keyboard_picker_script(pid, note))
    if result["returncode"] != 0 or result["stdout"] != "PICKER_KEYS_SENT_ONCE":
        raise runtime.WeChatRuntimeError(
            "picker shortcut not confirmed; no retry: " + json.dumps({
                "returncode": result["returncode"], "result": result["stdout"],
                "stderr": result.get("stderr", ""), "expected_window": note.title,
            }, ensure_ascii=False)
        )
    # Do not hold a System Events window proxy while the UI installs a modal
    # sheet. Each bounded read reacquires the exact native owner and children.
    return {"dispatch": "PICKER_KEYS_SENT_ONCE", **wait_for_owned_panel(pid, note.title)}


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
    result = open_picker_once(pid, note)
    return {
        "status": "PICKER_PREPARED_NOT_ATTACHMENT_ACCEPTED",
        "method": "COMMAND_O_NOTE_OWNED_AXSHEET_TEST_ONLY",
        "selection_placement": "COMMAND_DOWN_COMMAND_RIGHT_AFTER_EXACT_BODY_READBACK",
        "body_readback": readback,
        "body_comparison": comparison,
        "panel_identifier": "open-panel",
        "panel_observation": result,
        "automatic_action_retry_count": 0,
        "attachment_upload_count": 0,
        "production_integration_enabled": False,
    }


def _panel_key(pid: int, note: runtime.WindowIdentity, command: str) -> None:
    # command is an internal literal, never caller-provided input.
    script = f'''
    tell application "System Events"
        {runtime._process_selector(pid)}
        tell targetProc
            if frontmost is not true then return "NOT_FRONTMOST"
            if (name of window 1 as text) is not {_literal(note.title)} then return "NOTE_CHANGED"
            if (count of sheets of window 1) is not 1 then return "PANEL_CHANGED"
            if (value of attribute "AXIdentifier" of sheet 1 of window 1 as text) is not "open-panel" then return "PANEL_CHANGED"
            {command}
            return "KEY_SENT_ONCE"
        end tell
    end tell
    '''
    result = runtime._osascript(script)
    if result["returncode"] != 0 or result["stdout"] != "KEY_SENT_ONCE":
        raise runtime.WeChatRuntimeError("native picker navigation unknown; no retry: " + json.dumps({
            "returncode": result["returncode"], "result": result["stdout"],
            "stderr": result.get("stderr", ""),
        }, ensure_ascii=False))


def attach_pdf_once(pid: int, bundle_id: str, file_path: Path, *, expected_text: str) -> dict[str, Any]:
    """One exact AXOpen. Never resend a key/upload after ambiguous outcomes."""
    from .wechat_native_picker import NativePickerAX

    path = file_path.resolve()
    if bundle_id != runtime.TARGET_BUNDLE_ID or runtime.select_target_process()["pid"] != pid:
        raise runtime.WeChatRuntimeError("PDF picker requires verified WeChat2 process")
    if not path.is_file() or path.suffix.lower() != ".pdf" or path.stat().st_size <= 0:
        raise runtime.WeChatRuntimeError("nonempty local PDF required")
    if "[文件]" in expected_text or path.name in expected_text:
        raise runtime.WeChatRuntimeError("body must not contain attachment marker/filename")
    note = runtime.require_unique_note_window(pid)
    if note.role != runtime.NOTE_WINDOW_ROLE:
        raise runtime.WeChatRuntimeError("unique note window required")
    current, before = runtime.read_note_text(pid, bundle_id, note, max_attempts=1)
    comparison = runtime.compare_text_readback(expected_text, current)
    if not comparison["normalized_hash_match"] or "[文件]" in current:
        raise runtime.WeChatRuntimeError("PDF picker body mismatch/existing attachment")
    # Native title may update only after the editor/clipboard has settled.
    # Rebind after readback; never rely on the new draft's old `笔记` title.
    note = runtime.require_unique_note_window(pid)
    title = expected_text.splitlines()[0]
    if not note.title or not title.startswith(note.title):
        raise runtime.WeChatRuntimeError("post-readback note title does not match verified body")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    size = path.stat().st_size
    opened = open_picker_once(pid, note)
    _panel_key(pid, note, 'keystroke "g" using {command down, shift down}')
    time.sleep(0.5)
    with NativePickerAX(pid, note.title) as panel:
        panel.set_directory(path.parent)
    _panel_key(pid, note, "key code 36")  # directory navigation, not file open
    time.sleep(0.8)
    with NativePickerAX(pid, note.title) as panel:
        panel.exact_file(path)  # read-only proof before the sole file mutation
        if path.stat().st_size != size or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise runtime.WeChatRuntimeError("PDF changed before selection; no upload")
        dispatch = panel.open_exact_file_once(path)
    time.sleep(3)
    with NativePickerAX(pid, note.title) as panel:
        if panel.owned_panel(allow_pending=True) is not None:
            raise runtime.WeChatRuntimeError("file panel remains after AXOpen; no repeat allowed")
    current_after, after = runtime.read_note_text(
        pid, bundle_id, runtime.require_unique_note_window(pid), max_attempts=1
    )
    comparable = runtime.remove_attachment_placeholders(current_after, 1)
    after_comparison = runtime.compare_text_readback(expected_text, comparable)
    if not after_comparison["normalized_hash_match"]:
        raise runtime.WeChatRuntimeError("PDF attachment/body not verified before save; no retry")
    return {
        "status": "ATTACHMENT_VISIBLE",
        # Preserve the operation-bound checkpoint contract, not the old mechanism.
        "method": "TOOLBAR_FILE_PICKER_SINGLE_ATTEMPT",
        "trigger": "COMMAND_O_NOTE_OWNED_AXSHEET",
        "selection": "EXACT_CFURL_PATH_AXOPEN",
        "insertion": "COMMAND_DOWN_COMMAND_RIGHT_END_OF_BODY",
        "path": str(path), "filename": path.name, "byte_count": size, "sha256": digest,
        "open_panel_fingerprint": opened["status"],
        "panel_observation": opened,
        "file_dispatch": dispatch,
        "file_dispatch_resolved_by_readback": True,
        "placeholder_visible_before_save": True,
        "before_readback": before, "before_comparison": comparison,
        "after_readback": after, "after_comparison": after_comparison,
        "automatic_retry_count": 0,
    }
