from pathlib import Path
from unittest.mock import Mock, MagicMock

import pytest

from mikihouse_luyao import wechat_native_picker as native
from mikihouse_luyao import wechat_favorite_runtime as runtime


@pytest.mark.parametrize("code", [0, -25205])
def test_axopen_dispatch_is_never_itself_attachment_success(code):
    ax = object.__new__(native.NativePickerAX)
    ax.exact_file = Mock(return_value=123)
    ax.string = lambda s: s
    ax.ax = Mock()
    ax.ax.AXUIElementPerformAction.return_value = code
    result = ax.open_exact_file_once(Path("/private/tmp/quote.pdf"))
    assert result["status"] == "DISPATCH_REQUIRES_READBACK"
    assert result["api_returncode"] == code
    ax.ax.AXUIElementPerformAction.assert_called_once_with(123, "AXOpen")


def test_unobserved_axopen_error_fails_without_retry():
    ax = object.__new__(native.NativePickerAX)
    ax.exact_file = Mock(return_value=123)
    ax.string = lambda s: s
    ax.ax = Mock()
    ax.ax.AXUIElementPerformAction.return_value = -25211
    with pytest.raises(runtime.WeChatRuntimeError, match="do not retry"):
        ax.open_exact_file_once(Path("/private/tmp/quote.pdf"))
    assert ax.ax.AXUIElementPerformAction.call_count == 1


def test_panel_poll_reacquires_native_owner_without_resending_keys(monkeypatch):
    observations = iter([None, 42])
    instances = []
    def factory(*_):
        a = MagicMock()
        a.__enter__.return_value = a
        a.owned_panel.return_value = next(observations)
        instances.append(a)
        return a
    monkeypatch.setattr(native, "NativePickerAX", factory)
    monkeypatch.setattr(native.time, "sleep", lambda _: None)
    result = native.wait_for_owned_panel(123, "TEST")
    assert result["read_only_polls"] == 2
    assert len(instances) == 2
    for a in instances:
        a.open_exact_file_once.assert_not_called()


def test_panel_timeout_has_no_mutation_retry(monkeypatch):
    a = MagicMock()
    a.__enter__.return_value = a
    a.owned_panel.return_value = None
    monkeypatch.setattr(native, "NativePickerAX", lambda *_: a)
    with pytest.raises(runtime.WeChatRuntimeError, match="no retry"):
        native.wait_for_owned_panel(123, "TEST", timeout=0)
    a.open_exact_file_once.assert_not_called()


@pytest.mark.parametrize("titles,query", [(["笔记TEST", "笔记TEST other"], "TEST"),
                                        (["笔记FOREIGN"], "TEST"),
                                        (["笔记TEST"], "STALE")])
def test_favorites_focus_rejects_wrong_or_ambiguous_result(titles, query):
    ax = object.__new__(native.NativePickerAX)
    ax.owned_note = lambda: "main"
    ax.nodes = lambda _: ["list"]
    ax.array = lambda v: v
    ax.attr = lambda n, k: list(range(len(titles)))
    ax.value = lambda n, k: ("fav_detail_list" if k == "AXIdentifier" else
                             query if n == "list" else titles[n])
    with pytest.raises(runtime.WeChatRuntimeError):
        ax.focus_unique_favorite_result("TEST")


def test_pdf_close_never_uses_helper_or_main_file_menu(monkeypatch):
    main = runtime.WindowIdentity(1, "WeChat", "AXWindow")
    note = runtime.WindowIdentity(1, "TEST", "AXWindow")
    windows = iter([[note, main], [main]])
    monkeypatch.setattr(runtime, "get_windows", lambda _: next(windows))
    monkeypatch.setattr(runtime, "_raise_note", Mock())
    menu = Mock()
    monkeypatch.setattr(runtime, "press_menu_item", menu)
    a = MagicMock()
    a.__enter__.return_value = a
    a.close_note_once.return_value = {"method": "NOTE_OWNED_AXCLOSEBUTTON", "dispatch_count": 1}
    monkeypatch.setattr(native, "NativePickerAX", lambda *_: a)
    result = runtime.close_and_save_note(123, runtime.TARGET_BUNDLE_ID, note, native=True)
    assert result["native_close"]["dispatch_count"] == 1
    menu.assert_not_called()


def test_reopen_focuses_exact_list_before_home_enter(monkeypatch):
    main = runtime.WindowIdentity(1, "WeChat", "AXWindow")
    note = runtime.WindowIdentity(1, "TEST", "AXWindow")
    windows = iter([[main], [note, main]])
    monkeypatch.setattr(runtime, "get_windows", lambda _: next(windows))
    monkeypatch.setattr(runtime, "_main_window", lambda _: main)
    monkeypatch.setattr(runtime, "search_saved_note_candidates", lambda *a: {
        "candidate_lines": ["笔记TESTbody"], "favorites": {},
        "search_marker_sha256": "m", "search_tree_sha256": "t",
    })
    a = MagicMock()
    a.__enter__.return_value = a
    a.focus_unique_favorite_result.return_value = {"status": "EXACT_RESULT_LIST_FOCUSED"}
    monkeypatch.setattr(native, "NativePickerAX", lambda *_: a)
    def keys(script):
        a.focus_unique_favorite_result.assert_called_once_with("TEST")
        assert "key code 115" in script
        assert "key code 125" not in script
        return {"returncode": 0, "stdout": "UNIQUE_RESULT_OPEN_SENT"}
    monkeypatch.setattr(runtime, "_osascript", keys)
    reopened, ev = runtime.search_and_open_saved_note(123, runtime.TARGET_BUNDLE_ID, "TEST")
    assert reopened.title == note.title
    assert reopened.payload_title == "TEST"
    assert ev["result_focus"]["status"] == "EXACT_RESULT_LIST_FOCUSED"


@pytest.mark.parametrize("list_focus,row_focus,expected", [
    (True, False, "EXACT_RESULT_LIST"),
    (False, True, "UNIQUE_EXACT_NOTE_ROW"),
    (False, False, None),
])
def test_focus_accepts_only_verified_list_or_its_unique_row(list_focus, row_focus, expected):
    ax = object.__new__(native.NativePickerAX)
    ax.cf = Mock()
    ax.cf.CFBooleanGetValue.side_effect = bool
    ax.attr = lambda n, _: {"list": list_focus, "row": row_focus}[n]
    if expected is None:
        with pytest.raises(runtime.WeChatRuntimeError, match="did not acquire focus"):
            ax.verify_result_focus("list", "row")
    else:
        assert ax.verify_result_focus("list", "row") == expected
