from unittest.mock import Mock
import pytest
from mikihouse_luyao import wechat_favorite_runtime as r
from mikihouse_luyao import wechat_text_recovery as recovery
from mikihouse_luyao import wechat_pdf_keyboard as keyboard


def test_prefix_accepts_only_complete_chunk_and_newline_encoding():
    text = "A" * 300 + "\n" + "B" * 300 + "\n" + "C" * 300 + "\n"
    chunks = r.split_text_chunks(text, 512)
    prefix = "".join(chunks[:2])
    proof = recovery.prove_chunk_prefix(text, prefix.replace("\n", "\r"), 512)
    assert proof["sent_chunk_count"] == 2
    assert proof["prefix_character_count"] == len(prefix)
    for bad in [prefix[:-2], prefix.replace("B", "X", 1), prefix + " ", text, "", prefix + prefix]:
        with pytest.raises(r.WeChatRuntimeError):
            recovery.prove_chunk_prefix(text, bad, 512)


def test_scope_requires_pdf_pass_original_failure_bound_bundle_and_no_prior_resume():
    import copy
    cp = {"quote_date": "2026-09-24", "manifest_sha256": "m", "bundle_sha256": "b",
          "status": "FROZEN_RECONCILIATION_REQUIRED", "stages": {"pdf": {"status": "PASS"},
          "text": {"status": "FROZEN_AFTER_MUTATION_ATTEMPT", "payload_sha256": recovery.PAYLOAD_SHA,
                   "mutation_attempt_count": 1, "error": "WINDOW_NOT_UNIQUE 笔记"}}}
    pre = {k: cp[k] for k in ["quote_date", "manifest_sha256", "bundle_sha256"]}
    pre["payload_sha256"] = {"text": recovery.PAYLOAD_SHA}
    recovery.require_frozen_scope(cp, pre)
    for path, value in [(('stages','pdf','status'), 'FAILED'),
                        (('stages','text','error'), 'unknown paste outcome'),
                        (('stages','text','append_only_recovery'), {'status':'STARTED'}),
                        (('bundle_sha256',), 'changed')]:
        bad = copy.deepcopy(cp); target = bad
        for key in path[:-1]: target = target[key]
        target[path[-1]] = value
        with pytest.raises(r.WeChatRuntimeError): recovery.require_frozen_scope(bad, pre)


def test_initial_writer_drops_anonymous_binding_before_any_readback(monkeypatch):
    initial = r.WindowIdentity(1, '笔记', 'AXWindow', 'FORMAL', True)
    named = r.WindowIdentity(1, 'FORMAL', 'AXWindow', 'FORMAL')
    raised = []
    monkeypatch.setattr(r, '_raise_note', lambda p,b,n: raised.append(n))
    monkeypatch.setattr(r, '_clipboard_text', lambda: '')
    monkeypatch.setattr(r, '_set_clipboard_text', lambda _: None)
    monkeypatch.setattr(r.time, 'sleep', lambda _: None)
    monkeypatch.setattr(r, '_osascript', lambda _: {'returncode':0,'stdout':'PASTE_SENT'})
    wait = Mock(return_value=(named, {})); monkeypatch.setattr(keyboard,'wait_for_verified_note_title',wait)
    def read(p,b,n,expected):
        wait.assert_called_once()
        assert n is named and not n.new_draft
        return expected, {}, {'normalized_hash_match':True}
    monkeypatch.setattr(r,'read_note_text_until_match',read)
    r.write_note_text(123,'bundle',initial,'A'*5501)
    assert raised == [initial,named]


def test_append_ledger_precedes_single_paste_and_rejects_space_changes(monkeypatch):
    note=r.WindowIdentity(1,'FORMAL','AXWindow','FORMAL'); events=[]
    monkeypatch.setattr(r,'read_note_text',lambda *a,**k: ('prefix\r',{}))
    monkeypatch.setattr(r,'_clipboard_text',lambda:'')
    monkeypatch.setattr(r,'_set_clipboard_text',lambda _:None)
    monkeypatch.setattr(r,'_raise_note',lambda *a:None)
    monkeypatch.setattr(r.time,'sleep',lambda _:None)
    def paste(script):
        assert events[-1]['status']=='MUTATION_STARTED'
        assert 'keystroke "a"' not in script
        return {'returncode':0,'stdout':'PASTE_SENT'}
    action=Mock(side_effect=paste); monkeypatch.setattr(r,'_osascript',action)
    monkeypatch.setattr(r,'read_note_text_until_match',lambda *a:('prefix\nsuffix \n',{}, {'normalized_hash_match':True}))
    with pytest.raises(r.WeChatRuntimeError,match='cumulative readback mismatch'):
        r.append_note_chunks(123,'bundle',note,expected_prefix='prefix\n',remaining_chunks=['suffix\n','unsent'],strict_eol=True,progress=events.append)
    assert action.call_count==1 and len(events)==1


def test_recovery_has_no_pdf_ui_or_create_methods():
    import inspect
    source=inspect.getsource(recovery.recover_text_once)
    assert 'create_new_note(' not in source and '.save(' not in source
    assert 'attach_' not in source
    assert 'search_saved_note_candidates(pid, bundle, TITLE)' in source


def test_live_recovery_evidence_and_consumed_checkpoint_are_not_replayable():
    import hashlib
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    evidence = json.loads((root / 'docs/evidence/wechat_text_append_20260924.json').read_text())
    assert evidence['status'] == 'PASS_APPEND_ONLY_SAVED_REOPENED'
    assert evidence['appended_chunk_indices'] == list(range(3, 14))
    assert evidence['final_character_count'] == 70650
    assert evidence['reopened_full_hash_match']
    assert evidence['pdf_ui_access_count'] == evidence['resent_chunk_count'] == evidence['new_note_count'] == 0
    assert evidence['production_save_enabled'] is False
    current_binding = json.loads((root / 'docs/evidence/wechat_retained_window_20260925.json').read_text())
    for path, digest in evidence['source_sha256'].items():
        if path in current_binding['source_sha256']:
            digest = current_binding['source_sha256'][path]
        assert hashlib.sha256((root / path).read_bytes()).hexdigest() == digest
    directory = root / 'outputs/daily_quote/2026-09-24'
    cp = json.loads((directory / 'wechat_daily_production_checkpoint.json').read_text())
    before = json.loads((directory / 'wechat_text_append_before/wechat_daily_production_checkpoint.json').read_text())
    assert cp['status'] == 'PASS'
    assert cp['stages']['pdf'] == before['stages']['pdf']
    ledger = cp['stages']['text']['append_only_recovery']['chunk_ledger']
    for status in ('MUTATION_STARTED', 'READBACK_PASS'):
        assert [x['chunk_index'] for x in ledger if x['status'] == status] == list(range(3, 14))
    pre = {key: cp[key] for key in ('quote_date', 'manifest_sha256', 'bundle_sha256')}
    pre['payload_sha256'] = {'text': recovery.PAYLOAD_SHA}
    with pytest.raises(r.WeChatRuntimeError, match='no replay'):
        recovery.require_frozen_scope(cp, pre)
