from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/shijiu_browser_exact_capture.mjs"
REPORT = ROOT / "deliverables/shijiu_import/browser_exact_capture_readiness.json"
ANALYSIS = ROOT / "deliverables/shijiu_import/browser_exact_capture_analysis.json"


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
    assert 'context.waitForEvent("response"' in source
    assert 'context.on("page"' in source
    assert "context.pages().map" in source
    assert "await context.route" in source
    assert "/shopapi/Goods/index" in source
    assert "/shopapi/goods/getFormatInfo" in source
    assert 'args.mode === "readback"' in source
    assert 'good_code: ""' in source
    assert "skuIds.length === expectedSkuCount" in source
    assert "postReadOnlyForm" in source
    assert '"CAPTURED_EDIT_PRODUCT_ID"' in source
    assert "list_pages_scanned" in source
    assert "PENDING_READ_ONLY_RESUME" in source
    assert 'args.mode === "ui-readback"' in source
    assert '"NATIVE_UI_GOODS_INDEX_CAPTURED_EDIT_ID_AND_NAME"' in source
    assert ".click(" not in source
    assert ".fill(" not in source


def test_checked_in_browser_exact_report_is_verified_and_sanitized() -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    assert report["state"] == "BROWSER_EXACT_PRODUCT_VERIFIED_SKU_ID_NOT_EXPOSED"
    assert report["readiness"]["playwright"]["available"] is True
    assert report["readiness"]["cdp"]["available"] is False
    assert report["readiness"]["existing_chrome_attachable"] is False
    assert report["readiness"]["chrome_extension_status"] == "missing"
    assert report["current_capture"]["readback"]["product_id"]
    assert report["current_capture"]["operation_kind"] == "EDIT"
    assert report["current_capture"]["auth_context"]["query_body_token_equal"] is True
    assert report["current_capture"]["auth_context"]["values_included"] is False
    assert report["current_capture"]["readback"]["sku_ids"] == []
    assert report["current_capture"]["readback"]["goods_index_unique"] is True
    assert report["current_capture"]["readback"]["get_format_info_product_verified"] is True
    assert report["current_capture"]["readback"]["sku_structure_verified"] is True
    assert report["current_capture"]["readback"]["sku_id_exposed"] is False
    assert report["current_capture"]["readback"]["get_format_info_verified"] is False
    assert report["conclusion"]["missing_cookie_as_root_cause"] == "RULED_OUT_BY_PERSISTED_NATIVE_EDIT"
    assert report["conclusion"]["create_contract_captured"] is False
    assert report["safety"]["mikihouse_product_write_requests"] == 0
    assert report["safety"]["automatic_test_product_write_requests"] == 0
    assert report["safety"]["captured_human_test_product_write_requests"] == 1
    assert report["safety"]["read_only_requests"] == 2
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


def test_browser_exact_analysis_preserves_evidence_boundary() -> None:
    report = json.loads(ANALYSIS.read_text(encoding="utf-8"))
    assert report["state"] == "BROWSER_EXACT_PRODUCT_VERIFIED_SKU_ID_NOT_EXPOSED"
    assert report["authentication_and_tenant_evidence"]["cookie_present"] is False
    assert report["authentication_and_tenant_evidence"]["query_body_token_equal"] is True
    assert report["readback"]["product_id"] == "9357918"
    assert report["readback"]["sku_structure_verified"] is True
    assert report["readback"]["sku_id_exposed"] is False
    assert report["readback"]["sku_id_policy"] == "KEEP_NULL_DO_NOT_GUESS"
    assert report["evidence_boundary"]["current_request_is_edit_not_create"] is True
    assert report["evidence_boundary"]["create_silent_rejection_root_cause_proven"] is False
    assert report["safety"]["mikihouse_product_write_requests"] == 0
    assert report["safety"]["automatic_product_write_requests"] == 0
    assert report["safety"]["token_values_included"] is False
    assert report["safety"]["secret_values_included"] is False
