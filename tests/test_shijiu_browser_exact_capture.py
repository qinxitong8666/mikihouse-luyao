from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/shijiu_browser_exact_capture.mjs"
REPORT = ROOT / "deliverables/shijiu_import/browser_exact_capture_readiness.json"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_capture_helper_syntax_and_sensitive_shape_self_test() -> None:
    syntax = subprocess.run(
        ["node", "--check", str(SCRIPT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert syntax.returncode == 0, syntax.stderr
    result = subprocess.run(
        ["node", str(SCRIPT), "--self-test"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    output = json.loads(result.stdout)
    assert output == {"status": "PASS", "sensitive_values_included": False}


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_private_capture_path_inside_repo_is_rejected(tmp_path: Path) -> None:
    report = tmp_path / "should-not-exist.json"
    result = subprocess.run(
        [
            "node",
            str(SCRIPT),
            "--mode",
            "preflight",
            "--private-dir",
            str(ROOT / "private"),
            "--sanitized-report",
            str(report),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "outside the Git worktree" in result.stderr
    assert not report.exists()


def test_capture_source_uses_complete_headers_cdp_and_mikihouse_guard() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "request.allHeaders()" in source
    assert 'Network.requestWillBeSentExtraInfo' in source
    assert 'Network.requestWillBeSent' in source
    assert 'route.abort("blockedbyclient")' in source
    assert "MIKIHOUSE create payload was blocked before transmission" in source
    assert "page.waitForResponse" in source
    assert "/shopapi/Goods/index" in source
    assert "/shopapi/goods/getFormatInfo" in source
    assert ".click(" not in source
    assert ".fill(" not in source


def test_checked_in_preflight_report_proves_zero_target_writes() -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    assert report["state"] == "BLOCKED_EXISTING_CHROME_NOT_ATTACHABLE"
    assert report["readiness"]["playwright"]["available"] is True
    assert report["readiness"]["cdp"]["available"] is False
    assert report["readiness"]["existing_chrome_attachable"] is False
    assert report["readiness"]["chrome_extension_status"] == "missing"
    assert report["safety"]["mikihouse_product_write_requests"] == 0
    assert report["safety"]["automatic_test_product_write_requests"] == 0
    assert report["safety"]["token_values_included"] is False
    assert report["safety"]["secret_values_included"] is False
    assert report["safety"]["cookie_values_included"] is False
    baseline = report["comparisons"]["historical_wawu_vs_previous_mikihouse"]
    assert baseline["available"] is True
    assert baseline["method_equal"] is True
    assert baseline["endpoint_path_equal"] is True
    assert baseline["query_names_only_in_left"] == []
    assert baseline["query_names_only_in_right"] == []
    assert baseline["body_fields_only_in_left"] == []
    assert baseline["body_fields_only_in_right"] == []
    assert baseline["body_type_differences"] == []
    assert baseline["body_field_order_equal"] is True
