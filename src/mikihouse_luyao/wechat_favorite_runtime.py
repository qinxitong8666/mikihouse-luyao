from __future__ import annotations

import hashlib
import json
import plistlib
import re
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable


TARGET_BUNDLE_ID = "com.tencent.xinWeChot2"
TARGET_APP_PATH = "/Applications/微信2.app"
MAIN_WINDOW_TITLES = {"WeChat", "微信"}
NON_NOTE_WINDOW_TITLES = MAIN_WINDOW_TITLES | {"Window", ""}
AUXILIARY_WINDOW_TITLES = {"软件更新"}
NOTE_WINDOW_ROLE = "AXWindow"
PRODUCTION_CONFIRMATION = "CONFIRM_MIKIHOUSE_WECHAT_FAVORITE_PRODUCTION_SAVE"
TEST_TITLE_PREFIX = "MIKIHOUSE_TEST"


class WeChatRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class WindowIdentity:
    index: int
    title: str
    role: str


def normalize_wechat_text(text: str) -> str:
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in normalized.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def text_fingerprint(text: str) -> dict[str, Any]:
    normalized = normalize_wechat_text(text)
    lines = normalized.splitlines()
    return {
        "raw_character_count": len(text),
        "normalized_character_count": len(normalized),
        "utf8_byte_count": len(text.encode("utf-8")),
        "line_count": len(lines),
        "raw_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "normalized_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        "head_sha256": hashlib.sha256(normalized[:512].encode("utf-8")).hexdigest(),
        "tail_sha256": hashlib.sha256(normalized[-512:].encode("utf-8")).hexdigest(),
        "first_line": lines[0][:160] if lines else "",
        "last_line": lines[-1][:160] if lines else "",
    }


def compare_text_readback(expected: str, actual: str) -> dict[str, Any]:
    expected_fp = text_fingerprint(expected)
    actual_fp = text_fingerprint(actual)
    exact = expected_fp["raw_sha256"] == actual_fp["raw_sha256"]
    normalized = expected_fp["normalized_sha256"] == actual_fp["normalized_sha256"]
    return {
        "status": "EXACT_READBACK_MATCH" if exact else (
            "NORMALIZED_READBACK_MATCH" if normalized else "READBACK_MISMATCH"
        ),
        "exact_hash_match": exact,
        "normalized_hash_match": normalized,
        "truncated": actual_fp["normalized_character_count"] < expected_fp["normalized_character_count"],
        "expected": expected_fp,
        "actual": actual_fp,
    }


def remove_attachment_placeholders(text: str, attachment_count: int) -> str:
    """Remove only WeChat's exact trailing ``[文件]`` clipboard markers."""

    if attachment_count < 0:
        raise ValueError("attachment_count cannot be negative")
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines()
    for _ in range(attachment_count):
        if not lines or lines[-1] != "[文件]":
            raise WeChatRuntimeError("expected trailing WeChat attachment placeholder is missing")
        lines.pop()
    return "\n".join(lines) + ("\n" if lines else "")


def split_text_chunks(text: str, max_chars: int = 5500) -> list[str]:
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        hard_end = min(len(text), start + max_chars)
        end = hard_end
        if hard_end < len(text):
            newline = text.rfind("\n", start + 1, hard_end)
            if newline > start:
                end = newline + 1
        chunks.append(text[start:end])
        start = end
    if "".join(chunks) != text:
        raise AssertionError("chunk rejoin mismatch")
    return chunks


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _run(args: list[str], *, input_text: str | None = None) -> dict[str, Any]:
    result = subprocess.run(
        args, input=input_text, capture_output=True, text=True, check=False
    )
    return {
        "command": args,
        "returncode": result.returncode,
        "stdout": (result.stdout or "").strip(),
        "stderr": (result.stderr or "").strip(),
    }


def _osascript(script: str) -> dict[str, Any]:
    return _run(["osascript", "-e", script])


def _process_selector(pid: int) -> str:
    return f"set targetProc to first process whose unix id is {int(pid)}\n"


def _bundle_metadata(executable: str) -> dict[str, str]:
    path = Path(executable)
    app = next((parent for parent in (path, *path.parents) if parent.suffix == ".app"), None)
    if not app:
        return {"app_path": "", "bundle_id": "", "version": ""}
    plist = app / "Contents" / "Info.plist"
    data: dict[str, Any] = {}
    if plist.exists():
        with plist.open("rb") as handle:
            data = plistlib.load(handle)
    return {
        "app_path": str(app),
        "bundle_id": str(data.get("CFBundleIdentifier") or ""),
        "version": str(data.get("CFBundleShortVersionString") or ""),
    }


def select_target_process() -> dict[str, Any]:
    result = _run(["ps", "-axo", "pid=,comm="])
    candidates: list[dict[str, Any]] = []
    for line in result["stdout"].splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[1].endswith("/Contents/MacOS/WeChat"):
            continue
        metadata = _bundle_metadata(parts[1])
        if metadata["bundle_id"] == TARGET_BUNDLE_ID and metadata["app_path"] == TARGET_APP_PATH:
            candidates.append({"pid": int(parts[0]), "executable": parts[1], **metadata})
    if len(candidates) != 1:
        raise WeChatRuntimeError(f"expected exactly one 微信2 process, got {len(candidates)}")
    return candidates[0]


def get_windows(pid: int) -> list[WindowIdentity]:
    result = _osascript(
        f'''
        tell application "System Events"
            {_process_selector(pid)}
            tell targetProc
                set output to ""
                set idx to 0
                repeat with w in windows
                    set idx to idx + 1
                    try
                        set windowName to name of w as text
                    on error
                        set windowName to ""
                    end try
                    try
                        set windowRole to role of w as text
                    on error
                        set windowRole to ""
                    end try
                    set output to output & idx & "|||" & windowName & "|||" & windowRole & linefeed
                end repeat
                return output
            end tell
        end tell
        '''
    )
    if result["returncode"] != 0:
        raise WeChatRuntimeError(f"window inventory failed: {result['stderr']}")
    windows: list[WindowIdentity] = []
    for line in result["stdout"].splitlines():
        parts = line.split("|||", 2)
        if len(parts) == 3 and parts[0].isdigit():
            windows.append(WindowIdentity(int(parts[0]), parts[1], parts[2]))
    return windows


def _is_note_window(row: WindowIdentity) -> bool:
    return (
        row.role == NOTE_WINDOW_ROLE
        and row.title not in NON_NOTE_WINDOW_TITLES
        and row.title not in AUXILIARY_WINDOW_TITLES
    )


def _note_windows(pid: int) -> list[WindowIdentity]:
    return [row for row in get_windows(pid) if _is_note_window(row)]


def require_unique_note_window(pid: int) -> WindowIdentity:
    candidates = _note_windows(pid)
    if len(candidates) != 1:
        raise WeChatRuntimeError(f"expected one owned note window, got {len(candidates)}")
    return candidates[0]


def press_menu_item(pid: int, bundle_id: str, menu_title: str, item_title: str) -> dict[str, Any]:
    result = _osascript(
        f'''
        try
            tell application id "{bundle_id}" to activate
            delay 0.3
        end try
        tell application "System Events"
            {_process_selector(pid)}
            tell targetProc
                if not (exists menu bar item "{menu_title}" of menu bar 1) then return "MENU_NOT_FOUND"
                set targetMenuBarItem to menu bar item "{menu_title}" of menu bar 1
                perform action "AXPress" of targetMenuBarItem
                delay 0.2
                if not (exists menu item "{item_title}" of menu 1 of targetMenuBarItem) then return "MENU_ITEM_NOT_FOUND"
                set targetMenuItem to menu item "{item_title}" of menu 1 of targetMenuBarItem
                if enabled of targetMenuItem is false then return "MENU_ITEM_DISABLED"
                perform action "AXPress" of targetMenuItem
                delay 0.8
                return "MENU_ITEM_PRESSED|||1"
            end tell
        end tell
        '''
    )
    if result["returncode"] != 0 or result["stdout"] != "MENU_ITEM_PRESSED|||1":
        raise WeChatRuntimeError(
            f"menu press failed for {menu_title}/{item_title}: {result['stdout']} {result['stderr']}"
        )
    return result


def _main_window(pid: int) -> WindowIdentity:
    matches = [row for row in get_windows(pid) if row.title in MAIN_WINDOW_TITLES and row.role == NOTE_WINDOW_ROLE]
    if len(matches) != 1:
        raise WeChatRuntimeError(f"expected one WeChat main window, got {len(matches)}")
    return matches[0]


def collect_window_ax_text(pid: int, window: WindowIdentity, max_nodes: int = 5000) -> str:
    result = _osascript(
        f'''
        property maxNodes : {int(max_nodes)}

        on replaceText(findText, replacementText, sourceText)
            set oldDelims to AppleScript's text item delimiters
            set AppleScript's text item delimiters to findText
            set textItems to text items of sourceText
            set AppleScript's text item delimiters to replacementText
            set joinedText to textItems as text
            set AppleScript's text item delimiters to oldDelims
            return joinedText
        end replaceText

        on cleanText(valueText)
            try
                set cleaned to valueText as text
            on error
                set cleaned to ""
            end try
            set cleaned to my replaceText(linefeed, " ", cleaned)
            set cleaned to my replaceText(return, " ", cleaned)
            set cleaned to my replaceText("|||", " ", cleaned)
            if length of cleaned > 512 then set cleaned to text 1 thru 512 of cleaned
            return cleaned
        end cleanText

        on describeNode(nodeItem, idx)
            try
                set nodeRole to role of nodeItem as text
            on error
                set nodeRole to ""
            end try
            try
                set nodeName to name of nodeItem as text
            on error
                set nodeName to ""
            end try
            try
                set nodeDescription to description of nodeItem as text
            on error
                set nodeDescription to ""
            end try
            try
                set nodeValue to value of nodeItem as text
            on error
                set nodeValue to ""
            end try
            return nodeRole & "|||" & my cleanText(nodeName) & "|||" & my cleanText(nodeDescription) & "|||" & my cleanText(nodeValue) & linefeed
        end describeNode

        tell application "System Events"
            {_process_selector(pid)}
            tell targetProc
                if (count of windows) < {window.index} then return "WINDOW_NOT_FOUND"
                set targetWindow to window {window.index}
                set treeText to my describeNode(targetWindow, 0)
                try
                    set allItems to entire contents of targetWindow
                    set totalCount to count of allItems
                    set limitCount to maxNodes
                    if totalCount < limitCount then set limitCount to totalCount
                    if limitCount > 0 then
                        repeat with idx from 1 to limitCount
                            set treeText to treeText & my describeNode(item idx of allItems, idx)
                        end repeat
                    end if
                on error errMsg
                    set treeText to treeText & "children_error|||" & errMsg & linefeed
                end try
                return treeText
            end tell
        end tell
        '''
    )
    if result["returncode"] != 0 or result["stdout"] == "WINDOW_NOT_FOUND":
        raise WeChatRuntimeError(f"AX tree collection failed: {result['stderr']}")
    return result["stdout"]


def favorites_page_fingerprint(pid: int) -> dict[str, Any]:
    window = _main_window(pid)
    tree = collect_window_ax_text(pid, window)
    positives = [term for term in ("全部收藏", "最近使用", "图片与视频", "笔记", "文件", "小程序") if term in tree]
    # "聊天记录" is itself a valid Favorites category and is not a chat-page
    # signal.  Only controls that imply an active conversation are negative.
    negatives = [term for term in ("发送收藏", "发送文件", "聊天信息", "语音通话") if term in tree]
    # WeChat 4.1.6 exposes only a subset of the visible Favorites navigation
    # labels through AX.  "全部收藏" plus at least one Favorites-only category,
    # with no chat-only controls, is the strongest stable fingerprint observed
    # on the validated 微信2 build.
    passed = "全部收藏" in positives and len(positives) >= 2 and not negatives
    return {
        "status": "FAVORITES_PAGE_CONFIRMED" if passed else "FAVORITES_PAGE_NOT_CONFIRMED",
        "window": asdict(window),
        "positive_signals": positives,
        "negative_signals": negatives,
        "tree_sha256": hashlib.sha256(tree.encode("utf-8")).hexdigest(),
        "tree_line_count": len(tree.splitlines()),
    }


def navigate_to_favorites(pid: int, bundle_id: str) -> dict[str, Any]:
    current = favorites_page_fingerprint(pid)
    if current["status"] == "FAVORITES_PAGE_CONFIRMED":
        return current
    press_menu_item(pid, bundle_id, "窗口", "收藏")
    result = favorites_page_fingerprint(pid)
    if result["status"] != "FAVORITES_PAGE_CONFIRMED":
        raise WeChatRuntimeError("favorites page strong fingerprint failed")
    return result


def create_new_note(pid: int, bundle_id: str) -> tuple[WindowIdentity, dict[str, Any]]:
    fingerprint = navigate_to_favorites(pid, bundle_id)
    before = get_windows(pid)
    press_menu_item(pid, bundle_id, "文件", "新建笔记")
    after = get_windows(pid)
    before_keys = {(row.title, row.role) for row in before}
    new = [row for row in after if row.title == "笔记" and row.role == NOTE_WINDOW_ROLE and (row.title, row.role) not in before_keys]
    if len(new) != 1:
        before_note_count = len([row for row in before if row.title == "笔记" and row.role == NOTE_WINDOW_ROLE])
        after_notes = [row for row in after if row.title == "笔记" and row.role == NOTE_WINDOW_ROLE]
        if len(after_notes) == before_note_count + 1 and after_notes and after_notes[0].index == 1:
            new = [after_notes[0]]
    if len(new) != 1:
        raise WeChatRuntimeError(f"new note window not unique: {after}")
    return new[0], {"favorites": fingerprint, "windows_before": [asdict(row) for row in before], "windows_after": [asdict(row) for row in after]}


def _clipboard_text() -> str:
    result = subprocess.run(["pbpaste"], capture_output=True, check=False)
    if result.returncode != 0:
        raise WeChatRuntimeError("cannot read clipboard")
    return result.stdout.decode("utf-8")


def _set_clipboard_text(text: str) -> None:
    result = _run(["pbcopy"], input_text=text)
    if result["returncode"] != 0:
        raise WeChatRuntimeError("cannot set clipboard")


def _raise_note(pid: int, bundle_id: str, note: WindowIdentity) -> None:
    result = _osascript(
        f'''
        tell application id "{bundle_id}" to activate
        delay 0.2
        tell application "System Events"
            {_process_selector(pid)}
            tell targetProc
                set frontmost to true
                if (count of windows) < {note.index} then return "WINDOW_NOT_FOUND"
                perform action "AXRaise" of window {note.index}
                delay 0.2
                return (name of window 1 as text)
            end tell
        end tell
        '''
    )
    if result["returncode"] != 0 or result["stdout"] in {"", "WINDOW_NOT_FOUND", "WeChat", "微信"}:
        raise WeChatRuntimeError(f"note window is not frontmost: {result}")


def read_note_text(
    pid: int,
    bundle_id: str,
    note: WindowIdentity,
    *,
    max_attempts: int = 3,
    selection_delay_seconds: float = 1.5,
    clipboard_timeout_seconds: float = 8.0,
    clipboard_poll_seconds: float = 0.25,
) -> tuple[str, dict[str, Any]]:
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    if selection_delay_seconds < 0 or clipboard_timeout_seconds <= 0 or clipboard_poll_seconds <= 0:
        raise ValueError("readback timings must be positive")
    original = _clipboard_text()
    attempts: list[dict[str, Any]] = []
    try:
        for attempt_number in range(1, max_attempts + 1):
            sentinel = f"MIKIHOUSE_CLIPBOARD_SENTINEL_{uuid.uuid4().hex}"
            _set_clipboard_text(sentinel)
            _raise_note(pid, bundle_id, note)
            action = _osascript(
                f'''
                tell application "System Events"
                    {_process_selector(pid)}
                    tell targetProc
                        keystroke "a" using command down
                        delay {selection_delay_seconds:.3f}
                        keystroke "c" using command down
                        return "COPY_SENT"
                    end tell
                end tell
                '''
            )
            record: dict[str, Any] = {
                "attempt": attempt_number,
                "copy_action_status": action["stdout"],
                "copy_action_returncode": action["returncode"],
                "clipboard_changed": False,
            }
            if action["returncode"] == 0 and action["stdout"] == "COPY_SENT":
                deadline = time.monotonic() + clipboard_timeout_seconds
                while time.monotonic() < deadline:
                    copied = _clipboard_text()
                    if copied and copied != sentinel:
                        record["clipboard_changed"] = True
                        record["fingerprint"] = text_fingerprint(copied)
                        attempts.append(record)
                        return copied, {
                            "copy_status": "COPY_READ",
                            "attempt_count": attempt_number,
                            "selection_delay_seconds": selection_delay_seconds,
                            "clipboard_timeout_seconds": clipboard_timeout_seconds,
                            "attempts": attempts,
                            "fingerprint": record["fingerprint"],
                        }
                    time.sleep(clipboard_poll_seconds)
            attempts.append(record)
            if attempt_number < max_attempts:
                time.sleep(min(2.0, 0.5 * (2 ** (attempt_number - 1))))
        raise WeChatRuntimeError(
            f"note readback failed after {max_attempts} read-only attempts"
        )
    finally:
        _set_clipboard_text(original)


def read_note_text_until_match(
    pid: int,
    bundle_id: str,
    note: WindowIdentity,
    expected: str,
    *,
    max_attempts: int = 3,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Retry only the read-only copy path until the expected text is stable."""

    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    records: list[dict[str, Any]] = []
    last_actual = ""
    last_readback: dict[str, Any] = {}
    last_comparison: dict[str, Any] = {}
    for attempt in range(1, max_attempts + 1):
        last_actual, last_readback = read_note_text(pid, bundle_id, note)
        last_comparison = compare_text_readback(expected, last_actual)
        records.append({
            "attempt": attempt,
            "readback": last_readback,
            "comparison": last_comparison,
        })
        if last_comparison["normalized_hash_match"]:
            return last_actual, {
                "status": "EXPECTED_TEXT_STABLE",
                "attempt_count": attempt,
                "attempts": records,
            }, last_comparison
        if attempt < max_attempts:
            time.sleep(min(3.0, float(2 ** (attempt - 1))))
    return last_actual, {
        "status": "EXPECTED_TEXT_NOT_STABLE",
        "attempt_count": max_attempts,
        "attempts": records,
    }, last_comparison


def write_note_text(
    pid: int,
    bundle_id: str,
    note: WindowIdentity,
    text: str,
    *,
    chunk_chars: int = 5500,
) -> dict[str, Any]:
    chunks = split_text_chunks(text, chunk_chars)
    if not chunks:
        raise WeChatRuntimeError("refusing to write empty note")
    original_clipboard = _clipboard_text()
    expected = ""
    records: list[dict[str, Any]] = []
    try:
        for index, chunk in enumerate(chunks, 1):
            _raise_note(pid, bundle_id, note)
            _set_clipboard_text(chunk)
            select = 'keystroke "a" using command down\ndelay 0.15' if index == 1 else 'key code 125 using command down\nkey code 124 using command down\ndelay 0.15'
            action = _osascript(
                f'''
                tell application "System Events"
                    {_process_selector(pid)}
                    tell targetProc
                        {select}
                        keystroke "v" using command down
                        delay 0.45
                        return "PASTE_SENT"
                    end tell
                end tell
                '''
            )
            if action["returncode"] != 0 or action["stdout"] != "PASTE_SENT":
                raise WeChatRuntimeError(f"chunk {index} paste failed")
            time.sleep(min(3.0, max(0.5, len(chunk) / 5000)))
            expected += chunk
            actual, readback, comparison = read_note_text_until_match(
                pid, bundle_id, note, expected
            )
            records.append({
                "chunk_index": index,
                "chunk_character_count": len(chunk),
                "cumulative_expected_character_count": len(expected),
                "paste_status": "PASTE_SENT",
                "readback": readback,
                "comparison": comparison,
            })
            if not comparison["normalized_hash_match"]:
                raise WeChatRuntimeError(f"chunk {index} cumulative readback mismatch")
    finally:
        _set_clipboard_text(original_clipboard)
    final_actual, _, final = read_note_text_until_match(
        pid, bundle_id, note, text
    )
    if not final["normalized_hash_match"]:
        raise WeChatRuntimeError("final pre-save readback mismatch")
    return {
        "status": "PRE_SAVE_CONTENT_VERIFIED",
        "chunk_character_limit": chunk_chars,
        "chunk_count": len(chunks),
        "chunks": records,
        "final_comparison": final,
    }


def append_note_chunks(
    pid: int,
    bundle_id: str,
    note: WindowIdentity,
    *,
    expected_prefix: str,
    remaining_chunks: list[str],
) -> dict[str, Any]:
    """Resume only unsent chunks after a readback-proven exact prefix.

    This deliberately cannot infer a cursor or retry a prior paste.  Callers
    must provide the exact, already-read-back prefix and only chunks that have
    never been sent.
    """

    if not expected_prefix or not remaining_chunks:
        raise WeChatRuntimeError("resume requires a proven prefix and unsent chunks")
    current, _ = read_note_text(pid, bundle_id, note)
    prefix_comparison = compare_text_readback(expected_prefix, current)
    if not prefix_comparison["normalized_hash_match"]:
        raise WeChatRuntimeError("resume prefix does not match current note")
    original_clipboard = _clipboard_text()
    expected = expected_prefix
    records: list[dict[str, Any]] = []
    try:
        for offset, chunk in enumerate(remaining_chunks, 1):
            _raise_note(pid, bundle_id, note)
            _set_clipboard_text(chunk)
            action = _osascript(
                f'''
                tell application "System Events"
                    {_process_selector(pid)}
                    tell targetProc
                        key code 125 using command down
                        key code 124 using command down
                        delay 0.15
                        keystroke "v" using command down
                        delay 0.45
                        return "PASTE_SENT"
                    end tell
                end tell
                '''
            )
            if action["returncode"] != 0 or action["stdout"] != "PASTE_SENT":
                raise WeChatRuntimeError(f"resume chunk {offset} paste failed")
            time.sleep(min(3.0, max(0.5, len(chunk) / 5000)))
            expected += chunk
            actual, readback, comparison = read_note_text_until_match(
                pid, bundle_id, note, expected
            )
            records.append({
                "resume_chunk_offset": offset,
                "chunk_character_count": len(chunk),
                "cumulative_expected_character_count": len(expected),
                "paste_status": "PASTE_SENT_ONCE",
                "readback": readback,
                "comparison": comparison,
            })
            if not comparison["normalized_hash_match"]:
                raise WeChatRuntimeError(
                    f"resume chunk {offset} cumulative readback mismatch"
                )
    finally:
        _set_clipboard_text(original_clipboard)
    return {
        "status": "RESUME_UNSENT_CHUNKS_VERIFIED",
        "proven_prefix_comparison": prefix_comparison,
        "resumed_chunk_count": len(remaining_chunks),
        "chunks": records,
        "final_comparison": records[-1]["comparison"],
    }


def attach_file(pid: int, bundle_id: str, note: WindowIdentity, file_path: Path) -> dict[str, Any]:
    resolved = file_path.resolve()
    if not resolved.is_file():
        raise WeChatRuntimeError(f"attachment missing: {resolved}")
    original = _clipboard_text()
    try:
        _raise_note(pid, bundle_id, note)
        escaped = str(resolved).replace('\\', '\\\\').replace('"', '\\"')
        result = _osascript(
            f'''
            set the clipboard to (POSIX file "{escaped}") as alias
            tell application "System Events"
                {_process_selector(pid)}
                tell targetProc
                    key code 125 using command down
                    key code 124 using command down
                    keystroke "v" using command down
                    delay 4
                    return "FILE_PASTE_SENT"
                end tell
            end tell
            '''
        )
        if result["returncode"] != 0 or result["stdout"] != "FILE_PASTE_SENT":
            raise WeChatRuntimeError(f"attachment paste failed: {result}")
        tree = collect_window_ax_text(pid, require_unique_note_window(pid))
        filename_present = resolved.name in tree
        return {
            "status": "ATTACHMENT_VISIBLE" if filename_present else "ATTACHMENT_NOT_VISIBLE",
            "path": str(resolved),
            "filename": resolved.name,
            "byte_count": resolved.stat().st_size,
            "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
            "filename_present_in_ax_tree": filename_present,
            "page_fingerprint_sha256": hashlib.sha256(tree.encode("utf-8")).hexdigest(),
        }
    finally:
        _set_clipboard_text(original)


def close_and_save_note(pid: int, bundle_id: str, note: WindowIdentity) -> dict[str, Any]:
    before = get_windows(pid)
    _raise_note(pid, bundle_id, note)
    press_menu_item(pid, bundle_id, "文件", "关闭窗口")
    before_count = len([row for row in before if row.title not in NON_NOTE_WINDOW_TITLES])
    after = get_windows(pid)
    close_poll_attempt_count = 1
    after_count = len([row for row in after if row.title not in NON_NOTE_WINDOW_TITLES])
    while after_count != before_count - 1 and close_poll_attempt_count < 20:
        time.sleep(0.5)
        after = get_windows(pid)
        close_poll_attempt_count += 1
        after_count = len([row for row in after if row.title not in NON_NOTE_WINDOW_TITLES])
    if after_count != before_count - 1:
        raise WeChatRuntimeError("note did not close cleanly; save state unknown")
    return {
        "status": "NOTE_CLOSED_AUTO_SAVE_EXPECTED",
        "windows_before": [asdict(row) for row in before],
        "windows_after": [asdict(row) for row in after],
        "close_poll_attempt_count": close_poll_attempt_count,
        "save_prompt_observed": False,
    }


def search_and_open_saved_note(pid: int, bundle_id: str, marker: str) -> tuple[WindowIdentity, dict[str, Any]]:
    search_evidence = search_saved_note_candidates(pid, bundle_id, marker)
    candidate_lines = search_evidence["candidate_lines"]
    if len(candidate_lines) != 1:
        raise WeChatRuntimeError(f"saved note search is not unique: {len(candidate_lines)} candidates")
    before = get_windows(pid)
    opened = _osascript(
        f'''
        tell application "System Events"
            {_process_selector(pid)}
            tell targetProc
                key code 125
                delay 0.15
                key code 36
                delay 1
                return "UNIQUE_RESULT_OPEN_SENT"
            end tell
        end tell
        '''
    )
    if opened["returncode"] != 0 or opened["stdout"] != "UNIQUE_RESULT_OPEN_SENT":
        raise WeChatRuntimeError("cannot open unique saved-note search result")
    after = get_windows(pid)
    before_non_main = len([row for row in before if _is_note_window(row)])
    after_notes = [row for row in after if _is_note_window(row)]
    if len(after_notes) != before_non_main + 1:
        raise WeChatRuntimeError("saved note did not open as a new window")
    matching_notes = [
        row for row in after_notes
        if row.title and (marker.startswith(row.title) or row.title.startswith(marker))
    ]
    if len(matching_notes) != 1:
        raise WeChatRuntimeError(
            f"reopened note window is not uniquely identified: {len(matching_notes)}"
        )
    note = matching_notes[0]
    return note, {
        "status": "SAVED_NOTE_REOPENED_UNIQUE",
        "favorites": search_evidence["favorites"],
        "search_marker_sha256": search_evidence["search_marker_sha256"],
        "search_candidate_count": len(candidate_lines),
        "search_tree_sha256": search_evidence["search_tree_sha256"],
        "windows_before": [asdict(row) for row in before],
        "windows_after": [asdict(row) for row in after],
    }


def search_saved_note_candidates(pid: int, bundle_id: str, marker: str) -> dict[str, Any]:
    """Return a read-only exact-title search candidate inventory.

    It deliberately does not open, edit, or delete a result.  The same search
    semantics are shared by production collision preflight and post-save reopen.
    """

    favorites = navigate_to_favorites(pid, bundle_id)
    original = _clipboard_text()
    try:
        _set_clipboard_text(marker)
        search = _osascript(
            f'''
            tell application id "{bundle_id}" to activate
            delay 0.2
            tell application "System Events"
                {_process_selector(pid)}
                tell targetProc
                    set frontmost to true
                    keystroke "f" using command down
                    delay 0.2
                    keystroke "a" using command down
                    keystroke "v" using command down
                    delay 1.2
                    return "SEARCH_TYPED"
                end tell
            end tell
            '''
        )
        if search["returncode"] != 0 or search["stdout"] != "SEARCH_TYPED":
            raise WeChatRuntimeError("favorites search failed")
        main = _main_window(pid)
        tree = collect_window_ax_text(pid, main)
        candidate_lines = _exact_saved_note_candidate_lines(tree, marker)
        return {
            "status": "SAVED_NOTE_CANDIDATES_READ_ONLY",
            "favorites": favorites,
            "search_marker_sha256": hashlib.sha256(marker.encode("utf-8")).hexdigest(),
            "search_candidate_count": len(candidate_lines),
            "search_tree_sha256": hashlib.sha256(tree.encode("utf-8")).hexdigest(),
            "candidate_lines": candidate_lines,
        }
    finally:
        _set_clipboard_text(original)


def _exact_saved_note_candidate_lines(tree: str, marker: str) -> list[str]:
    """Return only exact saved-note title nodes from a Favorites search tree.

    WeChat exposes the search-results heading as a separate AX node whose text
    starts with a typographic quote followed by the query.  Substring matching
    therefore reports a false duplicate for every exact search.  A saved note
    title is counted only when one AX field equals the marker exactly.
    """

    candidates: list[str] = []
    for line in tree.splitlines():
        fields = line.split("|||", 3)
        if len(fields) == 4 and fields[0] != "AXTextField" and marker in fields[1:]:
            candidates.append(line)
    return candidates


def page_fingerprint(pid: int, window: WindowIdentity | None = None) -> dict[str, Any]:
    target = window or _main_window(pid)
    tree = collect_window_ax_text(pid, target)
    return {
        "window": asdict(target),
        "tree_sha256": hashlib.sha256(tree.encode("utf-8")).hexdigest(),
        "tree_line_count": len(tree.splitlines()),
        "contains_favorites_strong_signals": all(term in tree for term in ("全部收藏", "最近使用", "笔记")),
        "contains_chat_negative_signals": any(term in tree for term in ("发送收藏", "发送文件")),
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def validate_runtime_write_gate(
    payload: dict[str, Any],
    runtime_config: dict[str, Any],
    *,
    production: bool,
    confirmation: str | None = None,
    authorized_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Fail-closed authorization gate for the WeChat-only runtime sink.

    Runtime test notes must be unmistakably disposable.  Production saves need
    both a repository config switch and an exact invocation-time confirmation;
    neither can be inferred from the ordinary daily quote payload.
    """

    title = str(payload.get("title") or "")
    if production:
        if runtime_config.get("runtime_validation_status") != "PASS":
            raise WeChatRuntimeError("production save blocked: runtime validation is not PASS")
        if runtime_config.get("production_save_enabled") is not True:
            raise WeChatRuntimeError("production save blocked: config switch is disabled")
        if confirmation != PRODUCTION_CONFIRMATION:
            raise WeChatRuntimeError("production save blocked: exact confirmation is missing")
        expected_manifest = str(runtime_config.get("validated_manifest_sha256") or "")
        manifest_binding_mode = "VALIDATED_RUNTIME_SAMPLE"
        if authorized_manifest_sha256 is not None:
            if runtime_config.get("allow_fresh_daily_manifest_binding") is not True:
                raise WeChatRuntimeError("production save blocked: fresh manifest binding is disabled")
            if not re.fullmatch(r"[0-9a-f]{64}", authorized_manifest_sha256):
                raise WeChatRuntimeError("production save blocked: authorized manifest hash is invalid")
            expected_manifest = authorized_manifest_sha256
            manifest_binding_mode = "FRESH_DAILY_PRODUCTION_BUNDLE"
        if not expected_manifest or payload.get("source_manifest_sha256") != expected_manifest:
            raise WeChatRuntimeError("production save blocked: payload manifest is not validated")
        mode = "PRODUCTION"
    else:
        if not title.startswith(TEST_TITLE_PREFIX):
            raise WeChatRuntimeError("runtime test title must start with MIKIHOUSE_TEST")
        mode = "DISPOSABLE_TEST"
    attachments = [Path(value).name for value in payload.get("attachments") or []]
    return {
        "status": "RUNTIME_WRITE_GATE_PASS",
        "mode": mode,
        "title_sha256": hashlib.sha256(title.encode("utf-8")).hexdigest(),
        "source_manifest_sha256": payload.get("source_manifest_sha256"),
        "manifest_binding_mode": manifest_binding_mode if production else "DISPOSABLE_TEST",
        "attachment_filenames": attachments,
        "chat_send_allowed": False,
        "existing_favorite_mutation_allowed": False,
        "automatic_delete_allowed": False,
    }


class MacWeChatFavoriteSink:
    """Conservative WeChat Favorites sink using the validated 微信2 process.

    The sink creates a new note only. It never navigates to chats, never edits
    an existing favorite, and never deletes anything. Callers must separately
    choose test versus production authorization through ``save``.
    """

    def __init__(self, runtime_config: dict[str, Any]) -> None:
        self.runtime_config = dict(runtime_config)

    def save(
        self,
        payload: dict[str, Any],
        *,
        production: bool = False,
        confirmation: str | None = None,
        authorized_manifest_sha256: str | None = None,
        chunk_chars: int = 5500,
        reuse_existing_blank_test_note: bool = False,
    ) -> dict[str, Any]:
        gate = validate_runtime_write_gate(
            payload,
            self.runtime_config,
            production=production,
            confirmation=confirmation,
            authorized_manifest_sha256=authorized_manifest_sha256,
        )
        process = select_target_process()
        pid = int(process["pid"])
        bundle_id = str(process["bundle_id"])
        collision_preflight = None
        if production:
            collision_preflight = search_saved_note_candidates(
                pid, bundle_id, str(payload["title"])
            )
            if collision_preflight["search_candidate_count"] != 0:
                raise WeChatRuntimeError(
                    "production favorite title already exists or is ambiguous; refusing duplicate create"
                )
        if reuse_existing_blank_test_note:
            if production:
                raise WeChatRuntimeError("an existing blank note can only be reused for a runtime test")
            candidates = [row for row in _note_windows(pid) if row.title == "笔记"]
            if len(candidates) != 1:
                raise WeChatRuntimeError(
                    f"expected one explicitly verified blank test note, got {len(candidates)}"
                )
            note = candidates[0]
            creation = {
                "status": "REUSED_EXPLICITLY_VERIFIED_BLANK_TEST_NOTE",
                "window": asdict(note),
                "verification_requirement": "operator/Codex screenshot confirmed empty before invocation",
            }
        else:
            note, creation = create_new_note(pid, bundle_id)
        expected_text = f"{payload['title']}\n{payload.get('body') or ''}".rstrip() + "\n"
        write_evidence = write_note_text(
            pid,
            bundle_id,
            note,
            expected_text,
            chunk_chars=chunk_chars,
        )
        attachment_evidence = []
        for value in payload.get("attachments") or []:
            evidence = attach_file(pid, bundle_id, note, Path(value))
            if evidence["status"] != "ATTACHMENT_VISIBLE":
                raise WeChatRuntimeError("attachment was not visible before save")
            attachment_evidence.append(evidence)
        closed = close_and_save_note(pid, bundle_id, note)
        reopened, reopen_evidence = search_and_open_saved_note(pid, bundle_id, payload["title"])
        if attachment_evidence:
            actual_text, readback_evidence = read_note_text(pid, bundle_id, reopened)
            comparable_text = remove_attachment_placeholders(
                actual_text, len(attachment_evidence)
            )
            comparison = compare_text_readback(expected_text, comparable_text)
        else:
            actual_text, readback_evidence, comparison = read_note_text_until_match(
                pid, bundle_id, reopened, expected_text
            )
            comparable_text = actual_text
        if not comparison["normalized_hash_match"]:
            raise WeChatRuntimeError("reopened note text does not match payload")
        reopened_tree = collect_window_ax_text(pid, reopened)
        missing_attachments = [
            evidence["filename"]
            for evidence in attachment_evidence
            if evidence["filename"] not in reopened_tree
        ]
        if missing_attachments:
            raise WeChatRuntimeError(
                f"reopened note is missing attachment filenames: {missing_attachments}"
            )
        verification_close = close_and_save_note(pid, bundle_id, reopened)
        return {
            "status": "PASS",
            "gate": gate,
            "target_process": {
                "bundle_id": process["bundle_id"],
                "app_path": process["app_path"],
                "version": process["version"],
            },
            "creation": creation,
            "collision_preflight": collision_preflight,
            "write": write_evidence,
            "attachments": attachment_evidence,
            "close": closed,
            "reopen": reopen_evidence,
            "readback": readback_evidence,
            "comparison": comparison,
            "verification_close": verification_close,
            "reopened_page_fingerprint_sha256": hashlib.sha256(
                reopened_tree.encode("utf-8")
            ).hexdigest(),
        }

    def resume_test_note(
        self,
        payload: dict[str, Any],
        *,
        proven_chunk_count: int,
        chunk_chars: int = 5500,
    ) -> dict[str, Any]:
        """Resume one explicitly identified test note after a proven prefix.

        Only chunks after ``proven_chunk_count`` are sent. The prefix is
        re-read and hash-verified first; mutations are never retried.
        """

        gate = validate_runtime_write_gate(
            payload,
            self.runtime_config,
            production=False,
        )
        process = select_target_process()
        pid = int(process["pid"])
        bundle_id = str(process["bundle_id"])
        expected_text = f"{payload['title']}\n{payload.get('body') or ''}".rstrip() + "\n"
        chunks = split_text_chunks(expected_text, chunk_chars)
        if proven_chunk_count < 1 or proven_chunk_count >= len(chunks):
            raise WeChatRuntimeError("resume requires a strict non-final proven chunk prefix")
        candidates = [
            row for row in _note_windows(pid)
            if row.title and (
                str(payload["title"]).startswith(row.title)
                or row.title.startswith(str(payload["title"]))
            )
        ]
        if len(candidates) != 1:
            raise WeChatRuntimeError(
                f"expected one matching open test note for resume, got {len(candidates)}"
            )
        note = candidates[0]
        expected_prefix = "".join(chunks[:proven_chunk_count])
        _, prefix_readback, prefix_comparison = read_note_text_until_match(
            pid, bundle_id, note, expected_prefix
        )
        if not prefix_comparison["normalized_hash_match"]:
            raise WeChatRuntimeError("resume prefix strong readback failed")
        resumed = append_note_chunks(
            pid,
            bundle_id,
            note,
            expected_prefix=expected_prefix,
            remaining_chunks=chunks[proven_chunk_count:],
        )
        closed = close_and_save_note(pid, bundle_id, note)
        reopened, reopen_evidence = search_and_open_saved_note(
            pid, bundle_id, str(payload["title"])
        )
        actual_text, readback_evidence, comparison = read_note_text_until_match(
            pid, bundle_id, reopened, expected_text
        )
        if not comparison["normalized_hash_match"]:
            raise WeChatRuntimeError("reopened resumed note text does not match payload")
        reopened_tree = collect_window_ax_text(pid, reopened)
        return {
            "status": "PASS",
            "gate": gate,
            "target_process": {
                "bundle_id": process["bundle_id"],
                "app_path": process["app_path"],
                "version": process["version"],
            },
            "resume": {
                "proven_chunk_count": proven_chunk_count,
                "total_chunk_count": len(chunks),
                "prefix_readback": prefix_readback,
                "prefix_comparison": prefix_comparison,
                "remaining_chunks": resumed,
            },
            "close": closed,
            "reopen": reopen_evidence,
            "readback": readback_evidence,
            "comparison": comparison,
            "reopened_page_fingerprint_sha256": hashlib.sha256(
                reopened_tree.encode("utf-8")
            ).hexdigest(),
        }


def summarize_runtime_readiness(
    capacity: dict[str, Any],
    pdf_validation: dict[str, Any],
    text_validation: dict[str, Any],
) -> dict[str, Any]:
    full_characters = int(
        capacity.get("production_text_character_count")
        or capacity.get("full_text_character_count")
        or 0
    )
    maximum = int(capacity.get("maximum_verified_body_characters") or 0)
    production_text_status = capacity.get("production_text_status") or capacity.get("full_text_status")
    capacity_pass = (
        capacity.get("status") == "PASS"
        and production_text_status == "PASS"
        and maximum >= full_characters > 0
        and capacity.get("truncation_observed") is False
    )
    pdf_pass = (
        pdf_validation.get("status") == "PASS"
        and pdf_validation.get("reopened_title_present") is True
        and pdf_validation.get("reopened_body_present") is True
        and pdf_validation.get("reopened_attachment_filename_present") is True
    )
    text_pass = (
        text_validation.get("status") == "PASS"
        and text_validation.get("reopened_title_present") is True
        and text_validation.get("full_body_hash_match") is True
        and text_validation.get("truncation_observed") is False
    )
    ready = capacity_pass and pdf_pass and text_pass
    return {
        "schema_version": 1,
        "status": "PRODUCTION_READY_TWO_FAVORITES" if ready else "BLOCKED",
        "production_runtime_validated": ready,
        "exactly_two_favorites_contract": {
            "pdf_favorite": pdf_pass,
            "text_favorite": text_pass,
            "additional_favorites_allowed": False,
        },
        "capacity_ladder_passed": capacity_pass,
        "maximum_verified_text_characters": maximum,
        "full_text_character_count": full_characters,
        "pdf_attachment_runtime_passed": pdf_pass,
        "text_full_hash_readback_passed": text_pass,
        "production_save_enabled": False,
        "requires_explicit_future_enable_and_confirmation": True,
        "chat_send_count": 0,
        "existing_favorite_mutation_count": 0,
        "automatic_test_note_delete_count": 0,
        "shijiu_request_count": 0,
        "shijiu_mutation_count": 0,
        "writer_mutex_evidence_count": 0,
    }
