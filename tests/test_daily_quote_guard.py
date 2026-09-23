from pathlib import Path
import subprocess
import sys

import pytest

from mikihouse_luyao.daily_quote_guard import daily_output_lock, require_unprotected_quote_directory
from mikihouse_luyao.daily_quote_runner import run_daily_quote


@pytest.mark.parametrize("status", ["PASS", "FROZEN_RECONCILIATION_REQUIRED", "IN_PROGRESS", "corrupt"])
def test_generation_never_overwrites_checkpoint_bundle_or_crawls(tmp_path, monkeypatch, status):
    import mikihouse_luyao.daily_quote_runner as runner
    daily = tmp_path / "2026-09-23"
    daily.mkdir()
    checkpoint = daily / "wechat_daily_production_checkpoint.json"
    checkpoint.write_text(status)
    bundle = daily / "daily_quote_manifest.json"
    bundle.write_bytes(b"protected original")
    previous = tmp_path / "last_successful_manifest.json"
    previous.write_bytes(b"previous original")
    monkeypatch.setattr(runner, "fetch_all_storefront_products", lambda *a, **k: pytest.fail("crawl must not start"))
    with pytest.raises(ValueError, match="checkpoint 保护"):
        run_daily_quote(config_path=Path("config/daily_quote.json"), special_path=Path("special_skus_2026aw.csv"),
                        output_root=tmp_path, cache_dir=tmp_path / "cache", quote_date="2026-09-23")
    assert checkpoint.read_text() == status
    assert bundle.read_bytes() == b"protected original"
    assert previous.read_bytes() == b"previous original"


def test_output_lock_is_reentrant_but_other_process_fails_closed(tmp_path):
    with daily_output_lock(tmp_path):
        with daily_output_lock(tmp_path):
            code = "from pathlib import Path; from mikihouse_luyao.daily_quote_guard import daily_output_lock;\nwith daily_output_lock(Path(%r)): pass" % str(tmp_path)
            child = subprocess.run([sys.executable, "-c", code], text=True, capture_output=True, timeout=5)
            assert child.returncode != 0 and "正在运行" in child.stderr
    with daily_output_lock(tmp_path):
        pass


def test_broken_checkpoint_symlink_is_protection(tmp_path):
    (tmp_path / "wechat_daily_production_checkpoint.json").symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="checkpoint 保护"):
        require_unprotected_quote_directory(tmp_path)
