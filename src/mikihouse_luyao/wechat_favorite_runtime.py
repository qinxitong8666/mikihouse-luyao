from __future__ import annotations

import hashlib
import json
import plistlib
import re
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, replace
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
    payload_title: str | None = None
    new_draft: bool = False
    native_binding: str | None = None


_native_notes: dict[str, tuple[int, Any]] = {}


def retained_note_ax(pid: int, note: WindowIdentity):
    entry = _native_notes.get(note.native_binding or "")
    if not entry or entry[0] != pid:
        raise WeChatRuntimeError("missing invocation-local native note identity")
    entry[1].bound_window()
    return entry[1]


def _register_native_note(pid: int, note: WindowIdentity, ax) -> WindowIdentity:
    token = uuid.uuid4().hex
    _native_notes[token] = (pid, ax)
    return replace(note, native_binding=token)


def bind_existing_note(pid: int, note: WindowIdentity) -> WindowIdentity:
    if note.native_binding:
        retained_note_ax(pid, note)
        return note
    from .wechat_native_picker import NativePickerAX
    ax = NativePickerAX(pid, note.title)
    try:
        ax.bind_exact_title(note.payload_title or note.title)
        return _register_native_note(pid, note, ax)
    except Exception:
        ax.close()
        raise


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


def payload_title_matches(observed: str, expected: str) -> bool:
    # Observed WeChat 4.1.6 title: first 20 Unicode characters, not a fuzzy prefix.
    # Never accept an arbitrary shorter prefix or a longer near-match.
    return bool(expected) and observed in {expected, expected[:20]}


def payload_note_windows(pid: int, title: str) -> list[WindowIdentity]:
    return [WindowIdentity(w.index, w.title, w.role, title)
            for w in get_windows(pid)
            if w.role == NOTE_WINDOW_ROLE and payload_title_matches(w.title, title)]


def require_payload_note_window(pid: int, title: str, *, new_draft: bool = False) -> WindowIdentity:
    candidates = payload_note_windows(pid, title)
    if not candidates and new_draft:
        # Allowed only for the newly-created blank note, never existing notes.
        candidates = [WindowIdentity(w.index, w.title, w.role, title, True)
                      for w in get_windows(pid) if w.role == NOTE_WINDOW_ROLE and w.title == "笔记"]
    if len(candidates) != 1:
        raise WeChatRuntimeError(f"expected one payload-title note window, got {len(candidates)}")
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
    if window.payload_title:
        window = require_payload_note_window(pid, window.payload_title)
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
            tell application "System Events"
                try
                    set nodeRole to value of attribute "AXRole" of nodeItem as text
                on error
                    set nodeRole to ""
                end try
                try
                    set nodeName to value of attribute "AXTitle" of nodeItem as text
                on error
                    set nodeName to ""
                end try
                try
                    set nodeDescription to value of attribute "AXDescription" of nodeItem as text
                on error
                    set nodeDescription to ""
                end try
                try
                    set nodeValue to value of attribute "AXValue" of nodeItem as text
                on error
                    set nodeValue to ""
                end try
            end tell
            return nodeRole & "|||" & my cleanText(nodeName) & "|||" & my cleanText(nodeDescription) & "|||" & my cleanText(nodeValue) & linefeed
        end describeNode

        tell application "System Events"
            {_process_selector(pid)}
            tell targetProc
                if (count of windows) < {window.index} then return "WINDOW_NOT_FOUND"
                set targetWindow to window {window.index}
                if (name of targetWindow as text) is not {json.dumps(window.title, ensure_ascii=False)} then return "WINDOW_CHANGED"
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
    if result["returncode"] != 0 or result["stdout"] in {"WINDOW_NOT_FOUND", "WINDOW_CHANGED"}:
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
        raise WeChatRuntimeError(
            "无法确认微信收藏页面，已在写入前安全停止；"
            "请检查微信2是否停留在收藏页。不会自动重试或新建笔记。"
        )
    return result


def create_new_note(pid: int, bundle_id: str) -> tuple[WindowIdentity, dict[str, Any]]:
    from .wechat_native_picker import NativePickerAX
    fingerprint = navigate_to_favorites(pid, bundle_id)
    before = get_windows(pid)
    ax = NativePickerAX(pid, "")
    refs_before = ax.window_refs()
    try:
        press_menu_item(pid, bundle_id, "文件", "新建笔记")
        ax.bind_new_window(refs_before)
    except Exception:
        ax.close()
        raise
    after = get_windows(pid)
    # Native retained AX identity, not title/index, owns every subsequent step.
    note = WindowIdentity(0, ax.value(ax.bound_window(), "AXTitle"), NOTE_WINDOW_ROLE)
    return _register_native_note(pid, note, ax), {"favorites": fingerprint,
        "identity": "RETAINED_NATIVE_AX_WINDOW_DELTA", "windows_before": [asdict(row) for row in before],
        "windows_after": [asdict(row) for row in after]}


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
    if note.native_binding:
        ax = retained_note_ax(pid, note)
        result = _osascript(f'tell application id "{bundle_id}" to activate')
        if result["returncode"]:
            raise WeChatRuntimeError("cannot activate retained note application")
        ax.raise_bound()
        if ax.owned_panel(allow_pending=True) is not None:
            raise WeChatRuntimeError("retained note has modal sheet; no editor action")
        return
    if note.payload_title:
        note = require_payload_note_window(pid, note.payload_title, new_draft=note.new_draft)
    exact_title = json.dumps(note.title, ensure_ascii=False)
    result = _osascript(
        f'''
        tell application id "{bundle_id}" to activate
        delay 0.2
        tell application "System Events"
            {_process_selector(pid)}
            tell targetProc
                set frontmost to true
                set targetNotes to every window whose name is {exact_title}
                if (count of targetNotes) is not 1 then return "WINDOW_NOT_UNIQUE"
                perform action "AXRaise" of item 1 of targetNotes
                delay 0.2
                return (name of window 1 as text)
            end tell
        end tell
        '''
    )
    if result["returncode"] != 0 or result["stdout"] != note.title:
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
            if index == 1 and note.payload_title and note.new_draft and not note.native_binding:
                # The anonymous draft belongs to this invocation only before
                # its first paste. Never reacquire `笔记` after text was sent:
                # the editor updates its title asynchronously during readback.
                from .wechat_pdf_keyboard import wait_for_verified_note_title
                note, _ = wait_for_verified_note_title(pid, note.payload_title)
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
    strict_eol: bool = False,
    progress: Callable[[dict[str, Any]], None] | None = None,
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
    equal = lambda a, b: a.replace("\r\n", "\n").replace("\r", "\n") == b.replace("\r\n", "\n").replace("\r", "\n")
    if not prefix_comparison["normalized_hash_match"] or (strict_eol and not equal(expected_prefix, current)):
        raise WeChatRuntimeError("resume prefix does not match current note")
    original_clipboard = _clipboard_text()
    expected = expected_prefix
    records: list[dict[str, Any]] = []
    try:
        for offset, chunk in enumerate(remaining_chunks, 1):
            _raise_note(pid, bundle_id, note)
            _set_clipboard_text(chunk)
            if progress:
                progress({"resume_chunk_offset": offset, "status": "MUTATION_STARTED",
                          "chunk_sha256": hashlib.sha256(chunk.encode()).hexdigest(),
                          "chunk_character_count": len(chunk)})
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
            if not comparison["normalized_hash_match"] or (strict_eol and not equal(expected, actual)):
                raise WeChatRuntimeError(
                    f"resume chunk {offset} cumulative readback mismatch"
                )
            if progress:
                progress({"resume_chunk_offset": offset, "status": "READBACK_PASS",
                          "cumulative_eol_sha256": hashlib.sha256(expected.encode()).hexdigest(),
                          "cumulative_character_count": len(expected)})
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


def attach_file_with_toolbar_picker(
    pid: int,
    bundle_id: str,
    note: WindowIdentity,
    file_path: Path,
    *,
    expected_text: str,
) -> dict[str, Any]:
    """Compatibility entry point; uses Cmd+O and exact native AX file identity.

    No coordinate fallback, file-alias paste, global Open-button scan, or
    automatic mutation retry. Reacquire the unique note after its title changes.
    """
    from .wechat_pdf_keyboard import attach_pdf_once

    return attach_pdf_once(pid, bundle_id, file_path, expected_text=expected_text, note=note)


def close_and_save_note(
    pid: int, bundle_id: str, note: WindowIdentity, *, native: bool = False,
) -> dict[str, Any]:
    if note.native_binding:
        ax = retained_note_ax(pid, note)
        _raise_note(pid, bundle_id, note)
        close_action = ax.close_note_once()
        for poll in range(20):
            if not ax.bound_present():
                ax.close()
                _native_notes.pop(note.native_binding, None)
                return {"status": "NOTE_CLOSED_AUTO_SAVE_EXPECTED", "native_close": close_action,
                        "identity": "RETAINED_NATIVE_AX_WINDOW", "close_poll_attempt_count": poll + 1}
            time.sleep(0.5)
        raise WeChatRuntimeError("retained note did not close; save unknown; no retry")
    title = note.payload_title or note.title
    current = require_payload_note_window(pid, title)
    before = [current]
    close_action = None
    if native:
        from .wechat_native_picker import NativePickerAX
        _raise_note(pid, bundle_id, current)
        with NativePickerAX(pid, current.title) as ax:
            close_action = ax.close_note_once()
    else:
        _raise_note(pid, bundle_id, current)
        press_menu_item(pid, bundle_id, "文件", "关闭窗口")
    after = payload_note_windows(pid, title)
    close_poll_attempt_count = 1
    while after and close_poll_attempt_count < 20:
        time.sleep(0.5)
        after = payload_note_windows(pid, title)
        close_poll_attempt_count += 1
    if after:
        raise WeChatRuntimeError("note did not close cleanly; save state unknown")
    return {
        "status": "NOTE_CLOSED_AUTO_SAVE_EXPECTED",
        "windows_before": [asdict(row) for row in before],
        "windows_after": [asdict(row) for row in after],
        "close_poll_attempt_count": close_poll_attempt_count,
        "save_prompt_observed": False,
        "native_close": close_action,
    }


def search_and_open_saved_note(pid: int, bundle_id: str, marker: str) -> tuple[WindowIdentity, dict[str, Any]]:
    search_evidence = search_saved_note_candidates(pid, bundle_id, marker)
    candidate_lines = search_evidence["candidate_lines"]
    if len(candidate_lines) != 1:
        raise WeChatRuntimeError(f"saved note search is not unique: {len(candidate_lines)} candidates")
    before = payload_note_windows(pid, marker)
    if before:
        raise WeChatRuntimeError("payload note is already open; refusing duplicate reopen")
    from .wechat_native_picker import NativePickerAX
    main = _main_window(pid)
    with NativePickerAX(pid, main.title) as ax:
        focus = ax.focus_unique_favorite_result(marker)
    opened = _osascript(
        f'''
        tell application "System Events"
            {_process_selector(pid)}
            tell targetProc
                if frontmost is not true then return "PROCESS_NOT_FRONTMOST"
                if (name of window 1 as text) is not "WeChat" and (name of window 1 as text) is not "微信" then return "MAIN_WINDOW_CHANGED"
                key code 115
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
    after = payload_note_windows(pid, marker)
    matching_notes = after
    if len(matching_notes) != 1:
        raise WeChatRuntimeError(
            f"reopened note window is not uniquely identified: {len(matching_notes)}"
        )
    note = matching_notes[0]
    return note, {
        "status": "SAVED_NOTE_REOPENED_UNIQUE",
        "result_focus": focus,
        "favorites": search_evidence["favorites"],
        "search_marker_sha256": search_evidence["search_marker_sha256"],
        "search_candidate_count": len(candidate_lines),
        "search_tree_sha256": search_evidence["search_tree_sha256"],
        "windows_before": [asdict(row) for row in before],
        "windows_after": [asdict(row) for row in after],
    }


def search_saved_note_candidates(
    pid: int, bundle_id: str, marker: str, *, candidate_title: str | None = None,
) -> dict[str, Any]:
    """Return a read-only exact-title search candidate inventory.

    It deliberately does not open, edit, or delete a result.  The same search
    semantics are shared by production collision preflight and post-save reopen.
    """

    favorites = navigate_to_favorites(pid, bundle_id)
    main = _main_window(pid)
    original = _clipboard_text()
    try:
        _set_clipboard_text(marker)
        search = _osascript(
            f'''
            on isSearchField(nodeItem)
                tell application "System Events"
                    try
                        set nodeRole to value of attribute "AXRole" of nodeItem as text
                        if nodeRole is not "AXTextArea" and nodeRole is not "AXTextField" and nodeRole is not "AXSearchField" then return false
                        repeat with attributeName in {{"AXTitle", "AXDescription", "AXPlaceholderValue"}}
                            try
                                if (value of attribute (contents of attributeName) of nodeItem as text) is "搜索" then return true
                            end try
                        end repeat
                    end try
                end tell
                return false
            end isSearchField
            tell application id "{bundle_id}" to activate
            delay 0.2
            tell application "System Events"
                {_process_selector(pid)}
                tell targetProc
                    set frontmost to true
                    if (name of window {main.index} as text) is not "WeChat" and (name of window {main.index} as text) is not "微信" then return "MAIN_WINDOW_CHANGED"
                    set searchWindow to window {main.index}
                    perform action "AXRaise" of searchWindow
                    delay 0.2
                    set searchFields to {{}}
                    set allItems to entire contents of searchWindow
                    repeat with idx from 1 to count of allItems
                        if my isSearchField(item idx of allItems) then set end of searchFields to item idx of allItems
                    end repeat
                    if (count of searchFields) is not 1 then return "SEARCH_FIELD_NOT_UNIQUE"
                    set targetSearch to item 1 of searchFields
                    set expectedQuery to the clipboard as text
                    set value of attribute "AXFocused" of targetSearch to true
                    -- Reusing the unchanged query can retain a pre-save empty result.
                    set value of attribute "AXValue" of targetSearch to ""
                    delay 0.2
                    set value of attribute "AXValue" of targetSearch to expectedQuery
                    delay 0.3
                    if (value of attribute "AXValue" of targetSearch as text) is not expectedQuery then return "SEARCH_VALUE_MISMATCH"
                    key code 36
                    delay 1.2
                    return "SEARCH_TYPED"
                end tell
            end tell
            '''
        )
        if search["returncode"] != 0 or search["stdout"] != "SEARCH_TYPED":
            reason = search["stdout"] if search["stdout"] in {
                "MAIN_WINDOW_CHANGED", "SEARCH_FIELD_NOT_UNIQUE", "SEARCH_VALUE_MISMATCH"
            } else "ACCESSIBILITY_SEARCH_FAILED"
            raise WeChatRuntimeError(f"收藏搜索未完成（{reason}）；未确认标题不存在，禁止重建")
        main = _main_window(pid)
        tree = collect_window_ax_text(pid, main)
        candidate_lines = _exact_saved_note_candidate_lines(tree, candidate_title or marker)
        validate_saved_note_search_completion(tree, marker, len(candidate_lines))
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


def verify_saved_attachment_index(
    pid: int, bundle_id: str, title: str, expected_text: str, filenames: list[str],
) -> list[dict[str, Any]]:
    """After exact body+tail-marker reopen, prove filenames via Favorites index.

    This is additional evidence, never a substitute for reopen/body verification.
    Renderer AX often hides the file card. A filename absent from the complete
    body, indexed against the same unique title, proves an actual file attachment.
    """
    evidence = []
    for filename in filenames:
        if not filename or filename in expected_text:
            raise WeChatRuntimeError("attachment filename index proof is contaminated by body")
        result = search_saved_note_candidates(pid, bundle_id, filename, candidate_title=title)
        if result["search_candidate_count"] != 1:
            raise WeChatRuntimeError("saved attachment filename does not identify one task note")
        evidence.append({
            "method": "EXACT_FILENAME_INDEX_AND_REOPENED_BODY_WITH_TRAILING_FILE",
            "filename": filename, "search": result,
        })
    return evidence


def validate_saved_note_search_completion(tree: str, marker: str, candidate_count: int) -> None:
    """A query field alone cannot prove that a Favorites search completed."""
    # WeChat 4.1.6 exposes the highlighted query and the heading suffix as
    # adjacent AXStaticText nodes. Reassemble only adjacent static nodes,
    # never the editable search field or text from an unrelated result card.
    fragments: list[str] = []
    heading_verified = False
    heading_pattern = r"“" + re.escape(marker) + r"\s*”的搜索结果"
    for line in tree.splitlines():
        fields = line.split("|||", 3)
        if len(fields) != 4 or fields[0] != "AXStaticText":
            fragments = []
            continue
        values = [value for value in (fields[3], fields[1], fields[2])
                  if value and value != "missing value"]
        fragments.append(values[0] if values else "")
        fragments = fragments[-4:]
        if any(re.fullmatch(heading_pattern, "".join(fragments[start:]))
               for start in range(len(fragments))):
            heading_verified = True
    if not heading_verified:
        raise WeChatRuntimeError("收藏查询尚未确认完成或结果不是当前标题；禁止判定不存在")
    if candidate_count == 0 and not any(
        "无结果" in line.split("|||", 3)[1:] for line in tree.splitlines()
        if line.startswith("AXStaticText|||")
    ):
        raise WeChatRuntimeError("未读到明确的无结果状态；禁止凭空列表重建收藏")


def _exact_saved_note_candidate_lines(tree: str, marker: str) -> list[str]:
    """Inventory result cards, never the query input or selected-list label.

    WeChat 4.1.6 exposes a card as ``笔记<title><body preview>`` without a
    separator. Prefix matches are therefore conservative *candidates*, not
    binding proof. Full note readback must still match the payload. A longer
    title sharing the prefix blocks absence/rebuild rather than risking a
    duplicate. Each card counts, even when several cards have the same title.
    """
    candidates: list[str] = []
    title_observed = False
    for line in tree.splitlines():
        fields = line.split("|||", 3)
        if len(fields) != 4:
            continue
        if fields[0] == "AXList" and marker in fields[1:]:
            title_observed = True
        if fields[0] == "AXStaticText" and any(
            value.startswith("笔记" + marker) for value in fields[1:]
        ):
            candidates.append(line)
    if title_observed and not candidates:
        raise WeChatRuntimeError("收藏结果存在，但无法读取笔记卡片；已停止，不能判定标题不存在")
    if "children_error|||" in tree or not any(
        line.startswith("AX") for line in tree.splitlines()
    ):
        raise WeChatRuntimeError("收藏辅助功能结构读取不完整；已停止，不能判定标题不存在")
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

    def audit_pdf_draft_read_only(self, payload: dict[str, Any]) -> dict[str, Any]:
        process = select_target_process()
        pid, bundle = int(process["pid"]), str(process["bundle_id"])
        title = str(payload["title"])
        candidates = payload_note_windows(pid, title)
        if candidates:
            note = require_payload_note_window(pid, title)
        else:
            note, _ = search_and_open_saved_note(pid, bundle, title)
        expected = f"{title}\n{payload.get('body') or ''}".rstrip() + "\n"
        actual, readback = read_note_text(pid, bundle, note, max_attempts=1)
        comparison = compare_text_readback(expected, actual)
        if not comparison["normalized_hash_match"] or "[文件]" in actual:
            raise WeChatRuntimeError("title-scoped recovery requires exact body and zero attachment markers")
        return {"status": "VERIFIED_PAYLOAD_DRAFT_WITHOUT_ATTACHMENT", "window": asdict(note),
                "comparison": comparison, "readback": readback, "attachment_marker_count": 0,
                "other_note_body_reads": 0, "mutation_count": 0}

    def _require_pdf_runtime_acceptance(self, payload: dict[str, Any], production: bool) -> None:
        if (production and self.runtime_config.get("retained_ax_window_runtime_validation_status", "PASS")
                != "PASS"):
            raise WeChatRuntimeError("原生窗口绑定完整运行验收尚未通过，正式写入已阻止；不得重试冻结任务")
        if (production and payload.get("attachments") and
                self.runtime_config.get("coordinate_free_pdf_runtime_validation_status") != "PASS"):
            raise WeChatRuntimeError(
                "PDF自动附件完整程序验收尚未通过；正式保存/恢复已阻止，未创建或修改收藏"
            )

    def audit_titles_read_only(self, titles: list[str]) -> dict[str, Any]:
        """Inspect exact Favorite titles without opening, editing, or deleting notes."""

        if len(titles) != 2 or len(set(titles)) != 2:
            raise WeChatRuntimeError("daily title audit requires exactly two distinct titles")
        process = select_target_process()
        pid = int(process["pid"])
        bundle_id = str(process["bundle_id"])
        results: list[dict[str, Any]] = []
        for title in titles:
            evidence = search_saved_note_candidates(pid, bundle_id, title)
            results.append(
                {
                    "title_sha256": hashlib.sha256(title.encode("utf-8")).hexdigest(),
                    "search_candidate_count": int(
                        evidence.get("search_candidate_count") or 0
                    ),
                    "search_tree_sha256": evidence.get("search_tree_sha256"),
                    "search_marker_sha256": evidence.get("search_marker_sha256"),
                    "status": evidence.get("status"),
                }
            )
        return {
            "status": "READ_ONLY_TITLE_AUDIT_COMPLETE",
            "target_process": {
                "bundle_id": process["bundle_id"],
                "app_path": process["app_path"],
                "version": process["version"],
            },
            "results": results,
            "all_titles_absent": all(
                row["search_candidate_count"] == 0 for row in results
            ),
            "wechat_mutation_count": 0,
            "chat_send_count": 0,
            "shijiu_request_count": 0,
        }

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
        self._require_pdf_runtime_acceptance(payload, production)
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
            note = replace(note, payload_title=str(payload["title"]), new_draft=True)
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
            if production:
                attachment_contract = self.runtime_config.get(
                    "production_pdf_attachment"
                ) or {}
                if (
                    attachment_contract.get("strategy")
                    != "TOOLBAR_FILE_PICKER_SINGLE_ATTEMPT"
                    or attachment_contract.get("single_attempt_only") is not True
                    or attachment_contract.get("automatic_retry_enabled") is not False
                ):
                    raise WeChatRuntimeError(
                        "production PDF toolbar-picker contract is missing"
                    )
            evidence = attach_file_with_toolbar_picker(
                pid, bundle_id, note, Path(value), expected_text=expected_text,
            )
            if evidence["status"] != "ATTACHMENT_VISIBLE":
                raise WeChatRuntimeError("attachment was not visible before save")
            attachment_evidence.append(evidence)
        title_binding = {"status": "RETAINED_NATIVE_AX_WINDOW_TITLE_NOT_IDENTITY"}
        if not note.native_binding:
            from .wechat_pdf_keyboard import wait_for_verified_note_title
            note, title_binding = wait_for_verified_note_title(pid, str(payload["title"]))
        closed = close_and_save_note(pid, bundle_id, note, native=True)
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
        verification_close = close_and_save_note(pid, bundle_id, reopened, native=True)
        attachment_index = verify_saved_attachment_index(
            pid, bundle_id, payload["title"], expected_text,
            [item["filename"] for item in attachment_evidence],
        )
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
            "title_binding": title_binding,
            "attachments": attachment_evidence,
            "close": closed,
            "reopen": reopen_evidence,
            "readback": readback_evidence,
            "comparison": comparison,
            "verification_close": verification_close,
            "attachment_filename_readback": attachment_index,
            "reopened_page_fingerprint_sha256": hashlib.sha256(
                reopened_tree.encode("utf-8")
            ).hexdigest(),
        }

    def recover_existing_pdf_attachment(
        self,
        payload: dict[str, Any],
        *,
        production: bool,
        confirmation: str | None,
        authorized_manifest_sha256: str,
    ) -> dict[str, Any]:
        """Repair the one task-owned, text-only PDF note without recreating it.

        This is intentionally narrower than ``save``: one exact title must
        already exist, the text must match the frozen payload, no attachment
        may already be present, and the toolbar picker is invoked once.
        """

        self._require_pdf_runtime_acceptance(payload, production)
        gate = validate_runtime_write_gate(
            payload,
            self.runtime_config,
            production=production,
            confirmation=confirmation,
            authorized_manifest_sha256=authorized_manifest_sha256,
        )
        attachments = list(payload.get("attachments") or [])
        if len(attachments) != 1:
            raise WeChatRuntimeError("PDF recovery requires exactly one attachment")
        process = select_target_process()
        pid = int(process["pid"])
        bundle_id = str(process["bundle_id"])
        title = str(payload.get("title") or "")
        expected_text = f"{title}\n{payload.get('body') or ''}".rstrip() + "\n"
        search = search_saved_note_candidates(pid, bundle_id, title)
        if int(search.get("search_candidate_count") or 0) != 1:
            raise WeChatRuntimeError(
                "PDF recovery requires exactly one saved exact-title candidate"
            )

        open_notes = payload_note_windows(pid, title)
        if len(open_notes) == 1:
            note = open_notes[0]
            open_evidence = {
                "status": "REUSED_UNIQUE_OPEN_TASK_NOTE",
                "search_marker_sha256": search["search_marker_sha256"],
                "search_candidate_count": 1,
                "window": asdict(note),
            }
        elif len(open_notes) == 0:
            note, open_evidence = search_and_open_saved_note(
                pid, bundle_id, title
            )
        else:
            raise WeChatRuntimeError("PDF recovery found multiple matching open note windows")

        note = bind_existing_note(pid, note)
        before_text, before_readback = read_note_text(
            pid, bundle_id, note, max_attempts=1
        )
        before_comparison = compare_text_readback(expected_text, before_text)
        if not before_comparison["normalized_hash_match"]:
            raise WeChatRuntimeError("PDF recovery note text does not match payload")
        if normalize_wechat_text(before_text).endswith("[\u6587\u4ef6]"):
            raise WeChatRuntimeError("PDF recovery refused because an attachment already exists")

        attachment_path = Path(str(attachments[0])).resolve()
        attachment = attach_file_with_toolbar_picker(
            pid,
            bundle_id,
            note,
            attachment_path,
            expected_text=expected_text,
        )
        closed = close_and_save_note(
            pid, bundle_id, note, native=True,
        )
        reopened, reopen = search_and_open_saved_note(pid, bundle_id, title)
        actual_text, readback = read_note_text(
            pid, bundle_id, reopened, max_attempts=1
        )
        comparable = remove_attachment_placeholders(actual_text, 1)
        comparison = compare_text_readback(expected_text, comparable)
        if not comparison["normalized_hash_match"]:
            raise WeChatRuntimeError(
                "PDF recovery reopened readback did not prove text and attachment"
            )
        verification_close = close_and_save_note(pid, bundle_id, reopened, native=True)
        attachment_index = verify_saved_attachment_index(
            pid, bundle_id, title, expected_text, [attachment_path.name],
        )
        filename_visible = len(attachment_index) == 1
        return {
            "status": "PASS",
            "recovery_mode": "OPERATOR_AUTHORIZED_TOOLBAR_FILE_PICKER_SINGLE_ATTEMPT",
            "gate": gate,
            "target_process": {
                "bundle_id": process["bundle_id"],
                "app_path": process["app_path"],
                "version": process["version"],
            },
            "saved_note_search": search,
            "open_note": open_evidence,
            "text_before": before_readback,
            "text_before_comparison": before_comparison,
            "coordinate_free_attachment_evidence": attachment,
            "attachment": {
                "filename": attachment_path.name,
                "byte_count": attachment_path.stat().st_size,
                "sha256": hashlib.sha256(attachment_path.read_bytes()).hexdigest(),
                "selected_path": str(attachment_path),
                "visible_before_save": attachment["placeholder_visible_before_save"],
                "visible_after_reopen": filename_visible,
                "clipboard_placeholder_after_reopen": "[\u6587\u4ef6]",
                "method": attachment["method"],
            },
            "close": closed,
            "reopen": reopen,
            "readback": readback,
            "text_readback": comparison,
            "verification_close": verification_close,
            "attachment_filename_readback": attachment_index,
            "automatic_retry_count": 0,
            "chat_send_count": 0,
            "shijiu_request_count": 0,
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
