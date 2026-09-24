"""New-date ordinary App/CLI -> generator -> real Sink, with OS-only simulation.

No AppleScript/native AX/network call is allowed; artifacts live under tmp_path.
This is integration acceptance, NOT a new live WeChat acceptance claim.
"""
import hashlib
import json
import runpy
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from mikihouse_luyao import daily_quote_runner as generator
from mikihouse_luyao import daily_quote_images as images
from mikihouse_luyao import wechat_favorite_runtime as r
from mikihouse_luyao import wechat_native_picker as native
from mikihouse_luyao import wechat_text_recovery as recovery
from mikihouse_luyao.quote_assistant_app import build_production_command
from test_daily_quote import product, frozen_fx

ROOT = Path(__file__).resolve().parents[1]


def test_shared_image_cold_cache_atomic_publish_has_no_temp_collision(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    target = tmp_path / 'shared.jpg'
    barrier = Barrier(8)
    original = Path.replace
    def replace(path, destination):
        if destination == target:
            barrier.wait(timeout=10)
        return original(path, destination)
    monkeypatch.setattr(Path, 'replace', replace)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: images._atomic_write(target, b'complete-image'), range(8)))
    assert target.read_bytes() == b'complete-image'
    assert list(tmp_path.iterdir()) == [target]


class Desktop:
    """Emulate observable desktop boundaries, not Sink success decisions."""
    def __init__(self, monkeypatch, failure):
        self.notes = {}
        self.active = None
        self.clipboard = ''
        self.panel = False
        self.file = None
        self.events = []
        self.failure = failure
        self.lag = 0
        self.foreign = r.WindowIdentity(99, '用户其他笔记', 'AXWindow')
        monkeypatch.setattr(r.time, 'sleep', lambda _: None)
        monkeypatch.setattr(r, 'select_target_process', lambda: dict(pid=123, bundle_id=r.TARGET_BUNDLE_ID,
                                                                  app_path='/SIMULATED/微信2.app', version='fixture'))
        monkeypatch.setattr(r, 'get_windows', self.windows)
        monkeypatch.setattr(r, '_native_notes', {})
        monkeypatch.setattr(r, '_raise_note', self.raise_note)
        monkeypatch.setattr(r, '_clipboard_text', lambda: self.clipboard)
        monkeypatch.setattr(r, '_set_clipboard_text', lambda value: setattr(self, 'clipboard', value))
        monkeypatch.setattr(r, '_osascript', self.script)
        monkeypatch.setattr(r, 'create_new_note', self.create)
        monkeypatch.setattr(r, 'read_note_text', self.read)
        monkeypatch.setattr(r, 'search_saved_note_candidates', self.search)
        monkeypatch.setattr(r, 'search_and_open_saved_note', self.reopen)
        monkeypatch.setattr(r, 'collect_window_ax_text', lambda pid, note: self.read(pid, '', note)[0])
        monkeypatch.setattr(r, 'verify_saved_attachment_index', self.attachment_index)
        desktop = self

        class AX(native.NativePickerAX):
            # Real exact_file/open_exact_file_once scan and one-shot semantics.
            def __init__(self, pid, title):
                assert desktop.active and desktop.active['title'].startswith(title)
                self.owner = desktop.active
                self.ax = SimpleNamespace(AXUIElementPerformAction=self.perform)
            def bound_window(self):
                assert desktop.active is self.owner
                return self.owner
            def owned_note(self): return self.bound_window()
            def bound_present(self): return desktop.active is self.owner
            def close(self): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def string(self, value): return value
            def owned_panel(self, allow_pending=False): return 1 if desktop.panel else None
            def nodes(self, panel): return [1, 2, 3]
            def value(self, node, key):
                if key == 'AXURL':
                    if node == 2:
                        raise native.UnresolvableFileReference('file:///.file/id=unresolvable')
                    return desktop.file.as_uri() if node == 3 else ''
                return 'AXTextField' if key == 'AXRole' else ''
            def actions(self, node): return ['AXOpen'] if node == 3 else []
            def set_directory(self, directory):
                assert directory == desktop.file.parent
                desktop.events.append('directory_verified')
            def perform(self, node, action):
                assert node == 3 and action == 'AXOpen'
                desktop.events.append('AXOpen_once')
                if desktop.failure == 'picker': return -1
                desktop.active['text'] += '[文件]\n'
                desktop.active['attachment'] = desktop.file.name
                desktop.panel = False
                return 0
            def close_note_once(self):
                desktop.events.append(('close', desktop.active['title']))
                desktop.active = None
                return {'dispatch_count': 1}
        monkeypatch.setattr(native, 'NativePickerAX', AX)

    def windows(self, pid):
        if self.active is None: return [self.foreign]
        title = self.active['title']
        if self.lag:
            self.lag -= 1
            title = '笔记'
        own = r.WindowIdentity(1, title[:20], 'AXWindow')
        if self.failure == 'ambiguous' and title.endswith('文字版'):
            return [own, r.WindowIdentity(2, own.title, 'AXWindow'), self.foreign]
        return [own, self.foreign]

    def raise_note(self, pid, bundle, note):
        assert note.index != 99
        if self.active['text']:
            assert (note.native_binding or not note.new_draft) and note.payload_title == self.active['title']
        self.events.append(('raise', note.title))

    def create(self, *args):
        assert self.active is None
        self.active = dict(title='笔记', text='', attachment=None)
        self.events.append('create')
        note = r._register_native_note(123, r.WindowIdentity(1, '笔记', 'AXWindow'),
                                       native.NativePickerAX(123, '笔记'))
        return note, {'simulated': True, 'identity': 'RETAINED_NATIVE_AX_WINDOW_DELTA'}

    def script(self, script):
        assert 'click at' not in script
        if 'return "PASTE_SENT"' in script:
            if self.active['text']:
                assert 'keystroke "a"' not in script
            self.active['text'] += self.clipboard
            if self.active['title'] == '笔记':
                self.active['title'] = self.active['text'].splitlines()[0]
                self.notes[self.active['title']] = self.active
                self.lag = 1  # async title update after first paste
            self.events.append(('paste', self.active['title'], len(self.clipboard)))
            return dict(returncode=0, stdout='PASTE_SENT')
        if 'return "PICKER_KEYS_SENT_ONCE"' in script:
            assert script.index('key code 125') < script.index('key code 31')
            assert script.index('key code 124') < script.index('key code 31')
            assert 'every window whose name' not in script
            self.events.append('Command+O_once'); self.panel = True
            return dict(returncode=0, stdout='PICKER_KEYS_SENT_ONCE')
        if 'return "KEY_SENT_ONCE"' in script:
            assert self.panel
            return dict(returncode=0, stdout='KEY_SENT_ONCE')
        raise AssertionError('unexpected system action')

    def read(self, pid, bundle, note, **kwargs):
        assert note.index != 99 and (note.native_binding or not note.new_draft)
        assert note.payload_title == self.active['title']
        self.events.append(('read', self.active['title']))
        return self.active['text'].replace('\n', '\r'), {'simulated': True}

    def search(self, pid, bundle, title):
        self.events.append(('search', title))
        return {'search_candidate_count': int(title in self.notes)}

    def reopen(self, pid, bundle, title):
        assert self.active is None and title in self.notes
        if self.failure == 'ambiguous' and title.endswith('文字版'):
            raise r.WeChatRuntimeError('saved note search is not unique: 2 candidates')
        self.active = self.notes[title]
        self.lag = 0  # saved/reopened title, not the still-anonymous new editor
        self.events.append(('reopen', title))
        return r.WindowIdentity(1, title[:20], 'AXWindow', title), {'search_candidate_count': 1}

    def attachment_index(self, pid, bundle, title, body, filenames):
        assert self.active is None
        if not filenames: return []
        assert filenames == [self.notes[title]['attachment']]
        return [{'filename': filenames[0], 'exact_query_candidate_count': 1}]


@pytest.mark.parametrize('date,failure', [('2026-09-25', None), ('2026-10-01', None),
                                       ('2026-09-26', 'picker'), ('2026-09-27', 'ambiguous')])
def test_new_date_app_normal_entry_real_sink_no_recovery(tmp_path, monkeypatch, date, failure):
    core = runpy.run_path(str(ROOT / 'scripts/run_mikihouse_daily_production.py'))['main']
    desktop = Desktop(monkeypatch, failure)
    forbidden = Mock(side_effect=AssertionError('real OS/network/recovery forbidden'))
    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(recovery, 'recover_text_once', forbidden)
    monkeypatch.setitem(core.__globals__, 'recover_frozen_pdf_and_complete_daily_favorites', forbidden)
    # Simulate the already separately tested App permit boundary, without
    # issuing/consuming a real permit or changing the repository config.
    permit = Mock(return_value={'status': 'ACCEPTED', 'test_only': True})
    monkeypatch.setitem(core.__globals__, 'validate_and_consume_one_time_authorization', permit)
    config = json.loads((ROOT / 'config/daily_quote.json').read_text())
    config['crawl_minimum_product_count'] = 1  # small fixture, never production
    cfg = tmp_path / 'config.json'; cfg.write_text(json.dumps(config))
    runtime_config = json.loads((ROOT / 'config/wechat_favorite_runtime.json').read_text())
    runtime_config['retained_ax_window_runtime_validation_status'] = 'PASS'  # isolated fixture, not runtime evidence
    runtime_cfg = tmp_path / 'runtime.json'; runtime_cfg.write_text(json.dumps(runtime_config))
    rows = [product(f'88-{i:04d}-001', ['セカンドベビーシューズ', 'ベビー肌着', 'シャツ'][i % 3],
                    tags=['baby'] if i % 3 == 1 else []) for i in range(300)]
    crawl = Mock(return_value=(rows, {'storefront_product_count': len(rows)}))
    fx = Mock(return_value=frozen_fx())
    monkeypatch.setattr(generator, 'fetch_all_storefront_products', crawl)
    monkeypatch.setattr(generator, 'fetch_ecb_reference_rate', fx)
    image = tmp_path / 'source.jpg'; Image.new('RGB', (400, 400), 'white').save(image)
    monkeypatch.setattr(images, 'download_main_image', lambda *a, **kw: {
        'content_sha256': hashlib.sha256(image.read_bytes()).hexdigest(), 'source_path': str(image),
        'source_byte_count': image.stat().st_size})
    output = tmp_path / 'outputs'; daily = output / date
    desktop.file = (daily / f'MIKIHOUSE_{date}_报价全集.pdf').resolve()
    args = build_production_command(Path('python'), ROOT, r.PRODUCTION_CONFIRMATION)[2:] + [
        '--quote-date', date, '--output-root', str(output), '--thumbnail-cache', str(tmp_path / 'cache'),
        '--config', str(cfg), '--app-authorization-file', str(tmp_path / 'SIMULATED_ONLY')]
    args += ['--runtime-config', str(runtime_cfg)]
    result = core(args)
    cp = json.loads((daily / 'wechat_daily_production_checkpoint.json').read_text())
    assert crawl.call_count == fx.call_count == permit.call_count == 1
    assert 'recover_wechat_text_20260924' not in str(desktop.events)
    if failure:
        assert result == 1 and cp['status'] == 'FROZEN_RECONCILIATION_REQUIRED'
        before = list(desktop.events)
        assert core(args) == 1
        assert desktop.events == before  # checkpoint blocks every repeated GUI action
        if failure == 'picker': assert desktop.events.count('create') == 1
    else:
        assert result == 0 and cp['status'] == 'PASS'
        assert desktop.events.count('create') == 2
        assert desktop.events.count('Command+O_once') == desktop.events.count('AXOpen_once') == 1
        for kind in ['pdf', 'text']:
            payload = json.loads((daily / f'wechat_{kind}_favorite_payload.json').read_text())
            expected = (payload['title'] + '\n' + payload['body']).rstrip() + '\n'
            text = desktop.notes[payload['title']]['text']
            assert text == expected + ('[文件]\n' if kind == 'pdf' else '')
            ev = json.loads((daily / f'wechat_{kind}_favorite_production_validation.json').read_text())
            assert ev['comparison']['normalized_hash_match'] and ev['reopen']['search_candidate_count'] == 1
            if kind == 'text': assert ev['write']['chunk_count'] >= 2
            else: assert len(ev['attachments'][0]['file_dispatch']['file_scan']['skipped_unresolvable']) == 1
        before = list(desktop.events)
        assert core(args) == 0
        assert desktop.events == before  # same-date repeat never crawls or creates again
    assert crawl.call_count == fx.call_count == 1
    forbidden.assert_not_called()
    assert json.loads((ROOT / 'config/wechat_favorite_runtime.json').read_text())['production_save_enabled'] is False
