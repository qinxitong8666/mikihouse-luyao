"""Retained AX ownership survives title/ordering changes; no foreign body reads."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import hashlib
import json

import pytest

from mikihouse_luyao import wechat_favorite_runtime as r
from mikihouse_luyao import wechat_pdf_keyboard as picker
from mikihouse_luyao.wechat_native_picker import NativePickerAX


TITLE = "MIKI HOUSE 9月25日报价｜PDF版"
BODY = TITLE + "\n正文\n"
NOTE = r.WindowIdentity(0, "笔记", "AXWindow", TITLE, True, "retained-token")


def test_final_retained_runtime_evidence_and_default_authorization_gate():
    root=Path(__file__).resolve().parents[1]
    config=json.loads((root/'config/wechat_favorite_runtime.json').read_text())
    proof=json.loads((root/config['retained_ax_window_runtime_evidence_path']).read_text())
    assert config['production_save_enabled'] is False
    assert config['retained_ax_window_runtime_validation_status']=='PASS'
    assert proof['status']=='PASS_SAVED_REOPENED'
    assert proof['exact_title_counts']==[1,1]
    assert proof['pdf']['file_selection_dispatch_count']==1
    assert proof['pdf']['body_paste_count']==0
    assert proof['text']['normal_sink_used'] is True
    assert proof['text']['save_reopen_full_hash_match'] is True
    for path,digest in proof['source_sha256'].items():
        assert hashlib.sha256((root/path).read_bytes()).hexdigest()==digest
    for path in proof['historical_failures_preserved']:
        assert json.loads((root/path).read_text())['status'].startswith('BLOCKED')
    final=json.loads((root/'outputs/daily_quote/2026-09-25/wechat_final_readonly_validation.json').read_text())
    assert final['full_hash_match'] and final['exact_title_counts']==[1,1]
    assert final['expected_eol_sha256']==final['actual_eol_sha256']==proof['text']['eol_only_sha256']


def forbid_titles(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("write phase must not scan by payload title")
    for name in ("get_windows", "payload_note_windows", "require_payload_note_window"):
        monkeypatch.setattr(r, name, forbidden)
    monkeypatch.setattr(picker, "wait_for_verified_note_title", forbidden)


def test_native_identity_never_rebinds_to_replacement_or_same_title():
    ax = object.__new__(NativePickerAX)
    ax.cf = SimpleNamespace(CFEqual=lambda a,b: a == b)
    ax.bound_note = 7
    ax.window_refs = lambda: [9,7,11]
    ax.value = lambda n,k: "AXWindow" if k == "AXRole" else "笔记"
    assert ax.bound_window() == 7
    ax.window_refs = lambda: [9,11]
    with pytest.raises(r.WeChatRuntimeError, match="disappeared/replaced"):
        ax.bound_window()


def test_new_note_uses_native_delta_with_foreign_blank_note():
    ax = object.__new__(NativePickerAX)
    ax.cf = SimpleNamespace(CFEqual=lambda a,b: a == b)
    ax.window_refs = lambda: [3,2,1]
    ax.value = lambda n,k: "AXWindow" if k == "AXRole" else "笔记"
    ax.bind_new_window([1,2])
    assert ax.bound_note == 3
    with pytest.raises(r.WeChatRuntimeError, match="delta is not unique"):
        ax.bind_new_window([1])


def test_chunks_hold_original_window_even_when_title_never_changes(monkeypatch):
    forbid_titles(monkeypatch)
    monkeypatch.setattr(r.time, "sleep", lambda _: None)
    monkeypatch.setattr(r, "_clipboard_text", lambda: "old clipboard")
    monkeypatch.setattr(r, "_set_clipboard_text", Mock())
    focus = Mock()
    monkeypatch.setattr(r, "_raise_note", focus)
    paste = Mock(return_value={"returncode":0,"stdout":"PASTE_SENT"})
    monkeypatch.setattr(r, "_osascript", paste)
    read = Mock(side_effect=lambda pid,bundle,note,expected: (expected,{},
        {"normalized_hash_match":True}))
    monkeypatch.setattr(r, "read_note_text_until_match", read)
    result = r.write_note_text(123,r.TARGET_BUNDLE_ID,NOTE,BODY,chunk_chars=25)
    assert result["chunk_count"] > 1
    assert all(call.args[2] is NOTE for call in read.call_args_list)
    assert paste.call_count == result["chunk_count"]


def test_attachment_holds_same_native_owner_through_all_panel_steps(tmp_path,monkeypatch):
    forbid_titles(monkeypatch)
    monkeypatch.setattr(picker.time,"sleep",lambda _:None)
    path=tmp_path/'quote.pdf'; path.write_bytes(b'%PDF-test')
    ax=Mock()
    ax.owned_panel.return_value=None
    ax.open_exact_file_once.return_value={"dispatch_count":1}
    owner=Mock(return_value=ax)
    monkeypatch.setattr(r,"retained_note_ax",owner)
    monkeypatch.setattr(r,"select_target_process",lambda:{"pid":123})
    reads=Mock(side_effect=[(BODY,{}),(BODY+'[文件]\n',{})])
    monkeypatch.setattr(r,"read_note_text",reads)
    opened=Mock(return_value={"status":"NOTE_OWNED_OPEN_PANEL_CONFIRMED"})
    monkeypatch.setattr(picker,"open_picker_once",opened)
    monkeypatch.setattr(picker,"_panel_key",Mock())
    evidence=picker.attach_pdf_once(123,r.TARGET_BUNDLE_ID,path,expected_text=BODY,note=NOTE)
    assert evidence['status']=='ATTACHMENT_VISIBLE'
    ax.open_exact_file_once.assert_called_once_with(path)
    assert all(c.args[1] is NOTE for c in owner.call_args_list)
    assert all(c.args[2] is NOTE for c in reads.call_args_list)


def test_native_close_polls_original_ref_not_title(monkeypatch):
    forbid_titles(monkeypatch)
    ax=Mock(); ax.bound_present.side_effect=[True,False]
    monkeypatch.setattr(r,"retained_note_ax",lambda *a:ax)
    monkeypatch.setattr(r,"_raise_note",Mock())
    monkeypatch.setattr(r.time,"sleep",lambda _:None)
    result=r.close_and_save_note(123,r.TARGET_BUNDLE_ID,NOTE,native=True)
    assert result['identity']=='RETAINED_NATIVE_AX_WINDOW'
    ax.close_note_once.assert_called_once()


def test_retained_focus_has_no_title_selector(monkeypatch):
    forbid_titles(monkeypatch)
    ax=Mock()
    ax.owned_panel.return_value=None
    monkeypatch.setattr(r,"retained_note_ax",lambda *a:ax)
    action=Mock(return_value={'returncode':0})
    monkeypatch.setattr(r,"_osascript",action)
    r._raise_note(123,r.TARGET_BUNDLE_ID,NOTE)
    ax.raise_bound.assert_called_once()
    assert 'window' not in action.call_args.args[0]


def test_exact_owned_sheet_focus_is_valid_but_foreign_sheet_is_not():
    ax=object.__new__(NativePickerAX)
    ax.cf=SimpleNamespace(CFEqual=lambda a,b:a==b)
    ax.attr=lambda n,k:[8]
    ax.array=lambda v:v
    ax.value=lambda n,k:'AXSheet' if k=='AXRole' else 'open-panel'
    assert ax.focus_belongs_to_note(7,7)
    assert ax.focus_belongs_to_note(7,8)
    assert not ax.focus_belongs_to_note(7,9)
    ax.attr=lambda n,k:[8,9]
    assert not ax.focus_belongs_to_note(7,8)


def test_editor_read_must_not_type_into_open_panel(monkeypatch):
    forbid_titles(monkeypatch)
    ax=Mock(); ax.owned_panel.return_value=8
    monkeypatch.setattr(r,'retained_note_ax',lambda *a:ax)
    monkeypatch.setattr(r,'_osascript',Mock(return_value={'returncode':0}))
    with pytest.raises(r.WeChatRuntimeError,match='modal sheet'):
        r._raise_note(123,r.TARGET_BUNDLE_ID,NOTE)


def test_exact_nested_goto_sheet_is_owned_but_foreign_or_duplicate_is_not():
    ax=object.__new__(NativePickerAX)
    ax.cf=SimpleNamespace(CFEqual=lambda a,b:a==b)
    children={7:[8],8:[9],9:[]}
    identifiers={8:'open-panel',9:'GoToWindow',10:'GoToWindow'}
    ax.attr=lambda n,k:children.get(n,[])
    ax.array=lambda v:v
    ax.value=lambda n,k:'AXSheet' if k=='AXRole' else identifiers.get(n,'')
    assert ax.focus_belongs_to_note(7,9)
    assert not ax.focus_belongs_to_note(7,10)
    children[8]=[9,10]
    assert not ax.focus_belongs_to_note(7,9)
    children[8]=[9]; identifiers[9]='unrecognized-panel'
    assert not ax.focus_belongs_to_note(7,9)


def test_native_raise_dispatch_once_waits_only_readonly_focus(monkeypatch):
    from mikihouse_luyao import wechat_native_picker as native
    monkeypatch.setattr(native.time,'sleep',lambda _:None)
    ax=object.__new__(NativePickerAX)
    ax.bound_window=Mock(return_value=7)
    ax.string=lambda v:v
    dispatch=Mock(return_value=0)
    ax.ax=SimpleNamespace(AXUIElementPerformAction=dispatch)
    ax.owned_note=Mock(side_effect=[r.WeChatRuntimeError('retained note is not focused; no action'),7])
    ax.raise_bound()
    dispatch.assert_called_once_with(7,'AXRaise')
    assert ax.owned_note.call_count==2


def test_invalid_retained_token_does_not_fall_back(monkeypatch):
    forbid_titles(monkeypatch)
    with pytest.raises(r.WeChatRuntimeError,match='invocation-local'):
        r._raise_note(123,r.TARGET_BUNDLE_ID,NOTE)


def test_unaccepted_runtime_blocks_before_any_gui(monkeypatch):
    process=Mock()
    monkeypatch.setattr(r,'select_target_process',process)
    sink=r.MacWeChatFavoriteSink({'retained_ax_window_runtime_validation_status':'BLOCKED_RUNTIME_ACCEPTANCE'})
    with pytest.raises(r.WeChatRuntimeError,match='完整运行验收'):
        sink.save({'title':TITLE},production=True)
    process.assert_not_called()
