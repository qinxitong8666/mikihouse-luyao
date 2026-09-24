from unittest.mock import Mock

import pytest

from mikihouse_luyao import wechat_favorite_runtime as r

TITLE = "MIKI HOUSE 9月24日报价｜PDF版"


def test_only_exact_payload_or_observed_twenty_character_title_matches():
    assert r.payload_title_matches(TITLE, TITLE)
    assert r.payload_title_matches(TITLE[:20], TITLE)
    for wrong in ["笔记", "", TITLE[:19], TITLE + "副本", "MIKIHOUSE_TEST_2026-", "MIKI HOUSE 9月24日报价｜文字版"]:
        assert not r.payload_title_matches(wrong, TITLE)


def test_unrelated_windows_never_become_payload_candidates(monkeypatch):
    rows = [r.WindowIdentity(1, "MIKIHOUSE_TEST_2026-", "AXWindow"),
            r.WindowIdentity(2, "我的笔记", "AXWindow"),
            r.WindowIdentity(3, TITLE[:20], "AXWindow")]
    monkeypatch.setattr(r, "get_windows", lambda _: rows)
    read = Mock()
    monkeypatch.setattr(r, "read_note_text", read)
    result = r.require_payload_note_window(123, TITLE)
    assert result.index == 3 and result.payload_title == TITLE
    read.assert_not_called()
    rows.append(r.WindowIdentity(4, TITLE, "AXWindow"))
    with pytest.raises(r.WeChatRuntimeError, match="got 2"):
        r.require_payload_note_window(123, TITLE)
    read.assert_not_called()


def test_raise_rebinds_by_payload_not_stale_index(monkeypatch):
    monkeypatch.setattr(r, "get_windows", lambda _: [
        r.WindowIdentity(1, "用户窗口", "AXWindow"), r.WindowIdentity(7, TITLE[:20], "AXWindow")])
    action = Mock(return_value={"returncode": 0, "stdout": TITLE[:20]})
    monkeypatch.setattr(r, "_osascript", action)
    r._raise_note(123, r.TARGET_BUNDLE_ID, r.WindowIdentity(1, TITLE[:20], "AXWindow", TITLE))
    script = action.call_args.args[0]
    assert "every window whose name is" in script and TITLE[:20] in script
    assert "AXRaise\" of window 1" not in script
    assert "用户窗口" not in script


def test_closing_payload_ignores_foreign_window_count_changes(monkeypatch):
    target = r.WindowIdentity(2, TITLE[:20], "AXWindow", TITLE)
    inventories = iter([[target, r.WindowIdentity(1, "手工笔记", "AXWindow")],
                        [r.WindowIdentity(1, "手工笔记", "AXWindow"), r.WindowIdentity(2,"新手工笔记","AXWindow")]])
    monkeypatch.setattr(r, "get_windows", lambda _: next(inventories))
    raise_note = Mock()
    monkeypatch.setattr(r, "_raise_note", raise_note)
    monkeypatch.setattr(r, "press_menu_item", Mock(return_value={}))
    evidence = r.close_and_save_note(123, r.TARGET_BUNDLE_ID, target)
    assert evidence["status"] == "NOTE_CLOSED_AUTO_SAVE_EXPECTED"
    assert evidence["windows_after"] == []
    assert raise_note.call_args.args[2].payload_title == TITLE


def test_ax_body_tree_rebinds_target_and_checks_title_before_read(monkeypatch):
    monkeypatch.setattr(r, "get_windows", lambda _: [r.WindowIdentity(4, TITLE[:20], "AXWindow")])
    action = Mock(return_value={"returncode": 0, "stdout": "WINDOW_CHANGED", "stderr": ""})
    monkeypatch.setattr(r, "_osascript", action)
    with pytest.raises(r.WeChatRuntimeError):
        r.collect_window_ax_text(123, r.WindowIdentity(1, TITLE[:20], "AXWindow", TITLE))
    script = action.call_args.args[0]
    assert "set targetWindow to window 4" in script
    assert script.index('return "WINDOW_CHANGED"') < script.index("set treeText to my describeNode")
