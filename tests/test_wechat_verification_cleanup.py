from unittest.mock import Mock
import pytest
from mikihouse_luyao import wechat_favorite_runtime as r

TITLE='MIKI HOUSE 9月26日报价｜PDF版'
BODY=TITLE+'\n正文\n'
NOTE=r.WindowIdentity(1,TITLE[:20],'AXWindow',TITLE,False,'native')

def setup(monkeypatch,actual=BODY+'[文件]\n'):
    monkeypatch.setattr(r,'read_note_text',lambda *a,**k:(actual,{}))
    monkeypatch.setattr(r,'collect_window_ax_text',lambda *a:'target only')
    index=Mock(return_value=[{'filename':'quote.pdf','search':{'search_candidate_count':1}}])
    monkeypatch.setattr(r,'verify_saved_attachment_index',index)
    close=Mock(return_value={'status':'NOTE_CLOSED_AUTO_SAVE_EXPECTED'})
    monkeypatch.setattr(r,'close_and_save_note',close)
    return index,close

def verify():
    return r.verify_reopened_favorite(1,r.TARGET_BUNDLE_ID,NOTE,title=TITLE,expected_text=BODY,filenames=['quote.pdf'])

@pytest.mark.parametrize('error_type',[r.WeChatRuntimeError,RuntimeError])
def test_cleanup_failure_cannot_negate_complete_persistence_proof(monkeypatch,error_type):
    index,close=setup(monkeypatch)
    def cleanup(*a,**k):
        index.assert_called_once()
        raise error_type('retained note did not close; save unknown; no retry')
    close.side_effect=cleanup
    result=verify()
    assert result['save_status']=='SAVED_REOPEN_VERIFIED'
    assert result['full_eol_hash_match']
    assert result['verification_close']['status']=='CLEANUP_FAILED_AFTER_VERIFIED_SAVE'
    close.assert_called_once_with(1,r.TARGET_BUNDLE_ID,NOTE,native=True)

@pytest.mark.parametrize('actual',[BODY, BODY.replace('正文','错误')+'[文件]\n',BODY.rstrip()+'[文件]\n'])
def test_missing_attachment_or_body_never_becomes_cleanup_warning(monkeypatch,actual):
    index,close=setup(monkeypatch,actual)
    with pytest.raises(r.WeChatRuntimeError): verify()
    index.assert_not_called(); close.assert_not_called()

def test_filename_failure_aborts_before_cleanup(monkeypatch):
    index,close=setup(monkeypatch)
    index.side_effect=r.WeChatRuntimeError('missing exact filename')
    with pytest.raises(r.WeChatRuntimeError,match='missing exact filename'): verify()
    close.assert_not_called()

def test_verified_native_cleanup_once(monkeypatch):
    index,close=setup(monkeypatch)
    assert verify()['verification_close']['status']=='NOTE_CLOSED_AUTO_SAVE_EXPECTED'
    close.assert_called_once()

def test_unbound_reopened_window_cannot_be_used(monkeypatch):
    index,close=setup(monkeypatch)
    with pytest.raises(r.WeChatRuntimeError,match='retained native'):
        r.verify_reopened_favorite(1,r.TARGET_BUNDLE_ID,r.WindowIdentity(1,TITLE,'AXWindow'),title=TITLE,expected_text=BODY,filenames=['quote.pdf'])
    index.assert_not_called(); close.assert_not_called()


def test_initial_save_close_failure_is_fatal_not_cleanup_warning(monkeypatch):
    monkeypatch.setattr(r.MacWeChatFavoriteSink,'_require_pdf_runtime_acceptance',lambda *a:None)
    monkeypatch.setattr(r,'validate_runtime_write_gate',lambda *a,**k:{})
    monkeypatch.setattr(r,'select_target_process',lambda:dict(pid=1,bundle_id=r.TARGET_BUNDLE_ID))
    monkeypatch.setattr(r,'create_new_note',lambda *a:(NOTE,{}))
    monkeypatch.setattr(r,'write_note_text',lambda *a,**k:{})
    close=Mock(side_effect=r.WeChatRuntimeError('unknown first save'))
    monkeypatch.setattr(r,'close_and_save_note',close)
    reopen=Mock(); monkeypatch.setattr(r,'search_and_open_saved_note',reopen)
    with pytest.raises(r.WeChatRuntimeError,match='INITIAL_SAVE_CLOSE_FAILED'):
        r.MacWeChatFavoriteSink({}).save({'title':TITLE,'body':'正文','attachments':[]})
    close.assert_called_once(); reopen.assert_not_called()
