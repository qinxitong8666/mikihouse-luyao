#!/usr/bin/env python3
"""Offline fresh-date acceptance; no production permits or desktop mutations."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def protected():
    return {str(p.relative_to(ROOT)): sha(p) for directory in ('state', 'deliverables', 'outputs/daily_quote')
            for p in (ROOT / directory).rglob('*') if p.is_file() and not p.is_symlink()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'docs/evidence/wechat_first_run_acceptance.json')
    args = parser.parse_args()
    config = json.loads((ROOT / 'config/wechat_favorite_runtime.json').read_text())
    assert config['production_save_enabled'] is False
    before = protected()
    with tempfile.TemporaryDirectory(prefix='miki-first-run-') as directory:
        junit = Path(directory) / 'tests.xml'
        env = {**os.environ, 'MIKIHOUSE_VERIFY_OFFLINE': '1', 'PYTHONPYCACHEPREFIX': directory}
        tests = ['tests/test_wechat_first_run_acceptance.py', 'tests/test_wechat_pdf_keyboard.py',
                 'tests/test_wechat_native_picker.py', 'tests/test_wechat_payload_binding.py',
                 'tests/test_quote_assistant_app.py', 'tests/test_quote_assistant_authorization.py',
                 'tests/test_wechat_text_recovery.py']
        result = subprocess.run([sys.executable, '-m', 'pytest', '-q', *tests, f'--junitxml={junit}'],
                                cwd=ROOT, env=env, text=True, capture_output=True)
        print(result.stdout, end='')
        if result.returncode:
            raise SystemExit(result.stderr or 'offline acceptance failed')
        suite = ElementTree.parse(junit).getroot().find('testsuite')
        assert suite is not None and suite.get('failures') == suite.get('errors') == '0'
        count = int(suite.get('tests'))
    assert before == protected(), 'protected outputs changed'
    source_paths = [
        'macos/MikihouseQuoteAssistant/main.swift', 'scripts/run_mikihouse_daily_production.py',
        'src/mikihouse_luyao/quote_assistant_app.py', 'src/mikihouse_luyao/daily_quote_runner.py',
        'src/mikihouse_luyao/daily_quote_images.py', 'src/mikihouse_luyao/wechat_daily_production.py',
        'src/mikihouse_luyao/wechat_favorite_runtime.py', 'src/mikihouse_luyao/wechat_pdf_keyboard.py',
        'src/mikihouse_luyao/wechat_native_picker.py', 'tests/test_wechat_first_run_acceptance.py',
    ]
    report = {
        'status': 'PASS_OFFLINE_FRESH_DATE_NORMAL_PATH',
        'scope': 'NO_REAL_WECHAT_OR_SHIJIU_REQUEST; real orchestration/Sink, simulated OS/network edges',
        'test_dates': ['2026-09-25', '2026-10-01'], 'fixture_product_count_per_run': 300,
        'generation': 'REAL_MANIFEST_THUMBNAILS_PDF_TEXT_PREVIEWS_IN_TEMP_DIRECTORY',
        'external_edges': ['fixture full crawl', 'fixture frozen FX', 'fixture source image',
                           'simulated App permit validation; no real permit generated',
                           'in-memory desktop/clipboard/AX, including unresolvable CFURL'],
        'ordinary_entry': 'App build_production_command -> run_mikihouse_daily_production.main -> run_daily_quote -> save_daily_production_favorites -> MacWeChatFavoriteSink.save',
        'checks': {
            'crawl_and_fx_once_per_new_date': 'PASS', 'PDF_then_text': 'PASS',
            'first_paste_async_title_then_payload_only_binding': 'PASS',
            'single_Command_O_and_exact_CFURL_AXOpen': 'PASS',
            'unresolvable_unrelated_CFURL_skipped_not_bound': 'PASS',
            'attachment_inserted_at_body_end': 'PASS', 'multi_chunk_cumulative_readback': 'PASS',
            'separate_close_reopen_body_and_filename_validation': 'PASS',
            'same_date_replay_no_generation_no_GUI': 'PASS',
            'unknown_picker_or_ambiguous_title_freezes_without_retry': 'PASS',
            'other_notes_ignored': 'PASS', 'no_date_specific_recovery_called': 'PASS',
            'shared_image_cold_cache_concurrency': 'PASS',
        },
        'protected_file_count_unchanged': len(before), 'pytest_passed': count,
        'production_save_enabled': False, 'real_wechat_create_update_delete': 0,
        'real_chat_send_count': 0, 'shijiu_request_count': 0, 'real_authorization_generated': 0,
        'live_new_date_wechat_validation': 'NOT_RUN_BY_DESIGN_NO_WRITE_ACCEPTANCE',
        'prior_real_evidence': ['docs/evidence/wechat_cfurl_scan_20260924.json',
                                'docs/evidence/wechat_text_append_20260924.json'],
        'prior_real_evidence_hashes': {p: sha(ROOT / p) for p in [
            'outputs/daily_quote/2026-09-24/wechat_daily_production_checkpoint.json',
            'outputs/daily_quote/2026-09-24/wechat_daily_production_report.json']},
        'source_sha256': {p: sha(ROOT / p) for p in source_paths},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix('.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
