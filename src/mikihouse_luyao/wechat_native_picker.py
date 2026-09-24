"""Small, bounded macOS AX adapter for a note-owned native file panel.

No coordinate fallback, process-wide tree scan, or action retry. CFURL values
must be read through CoreFoundation (System Events cannot coerce them to text).
Imports/framework loading are lazy, so offline tests work on other platforms.
"""
from __future__ import annotations

import ctypes as C
import hashlib
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .wechat_favorite_runtime import WeChatRuntimeError


class UnresolvableFileReference(WeChatRuntimeError):
    """A CFURL exists but cannot be resolved. It is never file identity proof."""

    def __init__(self, raw_url: str):
        super().__init__("native file reference URL cannot resolve to path")
        self.raw_url_sha256 = hashlib.sha256(raw_url.encode("utf-8")).hexdigest()


class NativePickerAX:
    def __init__(self, pid: int, title: str):
        if sys.platform != "darwin":
            raise WeChatRuntimeError("native PDF picker requires macOS")
        self.cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.ax = C.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        self.refs: list[int] = []
        self.title = title
        p = C.c_void_p
        self._bind(self.cf, "CFRelease", None, [p])
        self._bind(self.cf, "CFEqual", C.c_bool, [p, p])
        self._bind(self.cf, "CFStringCreateWithCString", p, [p, C.c_char_p, C.c_uint32])
        self._bind(self.cf, "CFStringGetCString", C.c_bool, [p, C.c_char_p, C.c_long, C.c_uint32])
        self._bind(self.cf, "CFGetTypeID", C.c_ulong, [p])
        self._bind(self.cf, "CFBooleanGetValue", C.c_bool, [p])
        for name in ("CFStringGetTypeID", "CFArrayGetTypeID", "CFURLGetTypeID"):
            self._bind(self.cf, name, C.c_ulong, [])
        self._bind(self.cf, "CFURLGetString", p, [p])
        self._bind(self.cf, "CFURLCreateFilePathURL", p, [p, p, C.POINTER(p)])
        self._bind(self.cf, "CFArrayGetCount", C.c_long, [p])
        self._bind(self.cf, "CFArrayGetValueAtIndex", p, [p, C.c_long])
        self._bind(self.ax, "AXUIElementCreateApplication", p, [C.c_int])
        self._bind(self.ax, "AXUIElementSetMessagingTimeout", C.c_int, [p, C.c_float])
        self._bind(self.ax, "AXUIElementCopyAttributeValue", C.c_int, [p, p, C.POINTER(p)])
        self._bind(self.ax, "AXUIElementCopyActionNames", C.c_int, [p, C.POINTER(p)])
        self._bind(self.ax, "AXUIElementSetAttributeValue", C.c_int, [p, p, p])
        self._bind(self.ax, "AXUIElementPerformAction", C.c_int, [p, p])
        self.app = self._keep(self.ax.AXUIElementCreateApplication(pid))
        self.ax.AXUIElementSetMessagingTimeout(self.app, 2.0)

    @staticmethod
    def _bind(lib, name, result, args):
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = result, args

    def _keep(self, ref):
        if ref:
            self.refs.append(ref)
        return ref

    def close(self):
        for ref in reversed(self.refs):
            self.cf.CFRelease(ref)
        self.refs.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def string(self, text):
        return self._keep(self.cf.CFStringCreateWithCString(None, text.encode("utf-8"), 0x08000100))

    def text(self, ref):
        if not ref:
            return ""
        kind = self.cf.CFGetTypeID(ref)
        if kind == self.cf.CFURLGetTypeID():
            error = C.c_void_p()
            path_url = self._keep(self.cf.CFURLCreateFilePathURL(None, ref, C.byref(error)))
            self._keep(error.value)
            if not path_url:
                raise UnresolvableFileReference(self.text(self.cf.CFURLGetString(ref)))
            ref = self.cf.CFURLGetString(path_url)
        elif kind != self.cf.CFStringGetTypeID():
            return ""
        buf = C.create_string_buffer(32768)
        if not self.cf.CFStringGetCString(ref, buf, len(buf), 0x08000100):
            raise WeChatRuntimeError("AX string exceeds bounded buffer")
        return buf.value.decode("utf-8")

    def attr(self, node, name):
        value = C.c_void_p()
        error = self.ax.AXUIElementCopyAttributeValue(node, self.string(name), C.byref(value))
        if error in (-25205, -25212):  # unsupported attribute / no value
            return None
        if error:
            raise WeChatRuntimeError(f"AX read failed: {name} ({error})")
        return self._keep(value.value)

    def array(self, ref):
        if not ref:
            return []
        if self.cf.CFGetTypeID(ref) != self.cf.CFArrayGetTypeID():
            raise WeChatRuntimeError("AX collection has unexpected type")
        return [self.cf.CFArrayGetValueAtIndex(ref, n) for n in range(self.cf.CFArrayGetCount(ref))]

    def value(self, node, name):
        return self.text(self.attr(node, name))

    def owned_note(self):
        if getattr(self, "bound_note", None):
            note = self.bound_window()
            focused = self.attr(self.app, "AXFocusedWindow")
            frontmost = self.attr(self.app, "AXFrontmost")
            # macOS AXFocusedWindow becomes this exact child AXSheet while
            # the native file panel is open. It is not a replacement note.
            focused_owner = self.focus_belongs_to_note(note, focused)
            if (not focused_owner or not frontmost
                    or not self.cf.CFBooleanGetValue(frontmost)):
                raise WeChatRuntimeError("retained note is not focused; no action")
            return note
        windows = self.array(self.attr(self.app, "AXWindows"))
        notes = [w for w in windows if self.value(w, "AXTitle") == self.title]
        if len(notes) != 1 or not windows or self.value(windows[0], "AXTitle") != self.title:
            raise WeChatRuntimeError("native picker note identity is not unique")
        frontmost = self.attr(self.app, "AXFrontmost")
        if not frontmost or not self.cf.CFBooleanGetValue(frontmost):
            raise WeChatRuntimeError("WeChat2 is not frontmost; no picker action")
        return notes[0]

    def focus_belongs_to_note(self, note, focused):
        if not focused:
            return False
        if self.cf.CFEqual(note, focused):
            return True
        panels = [c for c in self.array(self.attr(note, "AXChildren"))
                  if self.value(c, "AXRole") == "AXSheet"
                  and self.value(c, "AXIdentifier") == "open-panel"]
        return len(panels) == 1 and bool(self.cf.CFEqual(panels[0], focused))

    def window_refs(self):
        # AXWindows CFArray stays retained in refs for the invocation lifetime.
        return self.array(self.attr(self.app, "AXWindows"))

    def bind_new_window(self, before):
        candidates = [w for w in self.window_refs()
                      if not any(self.cf.CFEqual(w, old) for old in before)
                      and self.value(w, "AXRole") == "AXWindow"
                      and self.value(w, "AXTitle") == "笔记"]
        if len(candidates) != 1:
            raise WeChatRuntimeError("new native AX window delta is not unique")
        self.bound_note = candidates[0]

    def bind_exact_title(self, title):
        from .wechat_favorite_runtime import payload_title_matches
        candidates = [w for w in self.window_refs()
                      if self.value(w, "AXRole") == "AXWindow"
                      and payload_title_matches(self.value(w, "AXTitle"), title)]
        if len(candidates) != 1:
            raise WeChatRuntimeError("existing native target window is not unique")
        self.bound_note = candidates[0]

    def bound_present(self):
        return any(self.cf.CFEqual(w, self.bound_note) for w in self.window_refs())

    def bound_window(self):
        matches = [w for w in self.window_refs() if self.cf.CFEqual(w, self.bound_note)]
        if len(matches) != 1 or self.value(matches[0], "AXRole") != "AXWindow":
            raise WeChatRuntimeError("retained native note disappeared/replaced; no rebind")
        return matches[0]

    def raise_bound(self):
        note = self.bound_window()
        error = self.ax.AXUIElementPerformAction(note, self.string("AXRaise"))
        if error:
            raise WeChatRuntimeError(f"retained native note AXRaise failed ({error})")
        self.owned_note()

    def owned_panel(self, *, allow_pending: bool = False):
        note = self.owned_note()
        # WeChat exposes AXSheet in AXChildren but omits AXSheets in the
        # native API. System Events synthesizes its `sheets` collection.
        sheets = [c for c in self.array(self.attr(note, "AXChildren"))
                  if self.value(c, "AXRole") == "AXSheet"]
        if not sheets and allow_pending:
            return None
        if len(sheets) != 1 or self.value(sheets[0], "AXIdentifier") != "open-panel":
            raise WeChatRuntimeError("native picker is not the note-owned open-panel")
        return sheets[0]

    def nodes(self, root):
        queue = [(root, 0)]
        result = []
        while queue:
            if len(result) >= 1500:
                raise WeChatRuntimeError("native panel exceeds AX traversal bound")
            node, depth = queue.pop(0)
            result.append(node)
            children = self.array(self.attr(node, "AXChildren"))
            if children and depth >= 18:
                raise WeChatRuntimeError("native panel exceeds AX depth bound")
            queue.extend((child, depth + 1) for child in children)
        return result

    def actions(self, node):
        value = C.c_void_p()
        error = self.ax.AXUIElementCopyActionNames(node, C.byref(value))
        if error:
            raise WeChatRuntimeError(f"AX actions unavailable ({error})")
        return [self.text(x) for x in self.array(self._keep(value.value))]

    def exact_file(self, path: Path):
        panel = self.owned_panel()
        matches = []
        self.last_file_scan = {"status": "SCANNING_READ_ONLY", "skipped_unresolvable": [],
                               "exact_match_count": 0, "mutation_count": 0}
        for index, node in enumerate(self.nodes(panel)):
            try:
                url = self.value(node, "AXURL")
            except UnresolvableFileReference as exc:
                # Never interpret an unresolved URL, node label, basename or
                # list position as the target. All other AX errors propagate.
                self.last_file_scan["skipped_unresolvable"].append({
                    "node_index": index, "role": self.value(node, "AXRole"),
                    "identifier": self.value(node, "AXIdentifier"),
                    "raw_url_sha256": exc.raw_url_sha256,
                    "reason": "UNRESOLVABLE_CFURL_NOT_IDENTITY_PROOF",
                })
                continue
            if exact_file_url(url, path):
                matches.append(node)
        self.last_file_scan["exact_match_count"] = len(matches)
        if len(matches) != 1 or "AXOpen" not in self.actions(matches[0]):
            self.last_file_scan["status"] = "BLOCKED_NO_UNIQUE_RESOLVED_AXOPEN_TARGET"
            raise WeChatRuntimeError("exact PDF AXURL/AXOpen is not unique; no file selected")
        self.last_file_scan.update(status="UNIQUE_RESOLVED_PATH_AND_AXOPEN",
                                   target_path_sha256=hashlib.sha256(str(path.resolve()).encode()).hexdigest())
        return matches[0]

    def set_directory(self, directory: Path):
        # GoToWindow is a child of this note's native panel, never a global match.
        panel = self.owned_panel()
        dialogs = [n for n in self.nodes(panel) if self.value(n, "AXIdentifier") == "GoToWindow"]
        if len(dialogs) != 1:
            raise WeChatRuntimeError("note-owned GoToWindow is not unique")
        fields = [n for n in self.nodes(dialogs[0]) if self.value(n, "AXIdentifier") == "PathTextField"]
        if len(fields) != 1:
            raise WeChatRuntimeError("GoToWindow PathTextField is not unique")
        focused = self.attr(fields[0], "AXFocused")
        if not focused or not self.cf.CFBooleanGetValue(focused):
            raise WeChatRuntimeError("directory field is not focused")
        expected = str(directory.resolve()) + "/"
        error = self.ax.AXUIElementSetAttributeValue(fields[0], self.string("AXValue"), self.string(expected))
        if error or self.value(fields[0], "AXValue") != expected:
            raise WeChatRuntimeError("directory navigation value not confirmed")

    def open_exact_file_once(self, path: Path):
        node = self.exact_file(path)
        error = self.ax.AXUIElementPerformAction(node, self.string("AXOpen"))
        if error not in (0, -25205):
            raise WeChatRuntimeError(f"file AXOpen returned unknown outcome ({error}); do not retry")
        # Observed -25205 even when the file panel closed and the attachment
        # was inserted. This is NOT success: caller must prove all effects.
        return {"api_returncode": error, "action": "AXOpen", "dispatch_count": 1,
                "status": "DISPATCH_REQUIRES_READBACK",
                "file_scan": getattr(self, "last_file_scan", None)}

    def close_note_once(self):
        note = self.owned_note()
        children = self.array(self.attr(note, "AXChildren"))
        if any(self.value(n, "AXRole") == "AXSheet" for n in children):
            raise WeChatRuntimeError("refusing close while note has a modal sheet")
        buttons = [n for n in children if self.value(n, "AXSubrole") == "AXCloseButton"]
        if len(buttons) != 1 or "AXPress" not in self.actions(buttons[0]):
            raise WeChatRuntimeError("note-owned AXCloseButton not unique")
        error = self.ax.AXUIElementPerformAction(buttons[0], self.string("AXPress"))
        if error:
            raise WeChatRuntimeError(f"note close outcome unknown ({error}); no retry")
        return {"method": "NOTE_OWNED_AXCLOSEBUTTON", "dispatch_count": 1}

    def focus_unique_favorite_result(self, marker: str):
        main = self.owned_note()
        lists = [n for n in self.nodes(main) if self.value(n, "AXIdentifier") == "fav_detail_list"]
        if len(lists) != 1 or self.value(lists[0], "AXTitle") != marker:
            raise WeChatRuntimeError("Favorites result list is not bound to exact query")
        rows = [n for n in self.array(self.attr(lists[0], "AXChildren"))
                if self.value(n, "AXTitle").strip()]
        if len(rows) != 1 or not self.value(rows[0], "AXTitle").startswith("笔记" + marker):
            raise WeChatRuntimeError("Favorites result list is not one exact-title note")
        yes = C.c_void_p.in_dll(self.cf, "kCFBooleanTrue").value
        error = self.ax.AXUIElementSetAttributeValue(lists[0], self.string("AXFocused"), yes)
        if error:
            raise WeChatRuntimeError(f"Favorites focus dispatch failed ({error}); no retry")
        focus_target = self.verify_result_focus(lists[0], rows[0])
        return {"status": "EXACT_RESULT_LIST_FOCUSED", "candidate_count": 1,
                "list_identifier": "fav_detail_list", "query": marker,
                "focus_target": focus_target}

    def verify_result_focus(self, result_list, exact_row):
        # WeChat can delegate focus to the sole result row instead of keeping
        # it on AXList. Both are within the already verified exact query tree;
        # a search box/foreign row is never accepted. This performs no action.
        for name, node in (("EXACT_RESULT_LIST", result_list), ("UNIQUE_EXACT_NOTE_ROW", exact_row)):
            focused = self.attr(node, "AXFocused")
            if focused and self.cf.CFBooleanGetValue(focused):
                return name
        raise WeChatRuntimeError("Favorites result list/unique note row did not acquire focus")


def exact_file_url(url: str, path: Path) -> bool:
    parts = urlsplit(url)
    return (parts.scheme == "file" and parts.netloc in ("", "localhost")
            and not parts.query and not parts.fragment
            and unquote(parts.path) == str(path.resolve()))


def wait_for_owned_panel(pid: int, title: str, *, timeout: float = 5.0) -> dict:
    """Fresh native references on every read. Never re-dispatch the shortcut."""
    deadline = time.monotonic() + timeout
    polls = 0
    while True:
        polls += 1
        with NativePickerAX(pid, title) as ax:
            panel = ax.owned_panel(allow_pending=True)
            if panel is not None:
                return {"status": "NOTE_OWNED_OPEN_PANEL_CONFIRMED", "read_only_polls": polls,
                        "identifier": "open-panel", "owner_pid": pid, "note_title": title}
        if time.monotonic() >= deadline:
            raise WeChatRuntimeError(f"native open-panel absent after {polls} read-only polls; no retry")
        time.sleep(0.2)
