from __future__ import annotations

import json
import subprocess
from pathlib import Path

from mikihouse_luyao.shijiu_session_audit import build_session_audit


ROOT = Path(__file__).resolve().parents[1]


def _write_fixture_tree(tmp_path: Path, *, cookie: bool, complete_headers: bool):
    miki = tmp_path / "miki"
    wawu = tmp_path / "wawu"
    (miki / "deliverables/shijiu_import").mkdir(parents=True)
    wawu.mkdir()
    subprocess.run(["git", "init", "-q", str(miki)], check=True)
    subprocess.run(["git", "init", "-q", str(wawu)], check=True)
    subprocess.run(
        [
            "git", "-C", str(miki), "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid", "commit", "--allow-empty",
            "-qm", "miki",
        ],
        check=True,
    )
    backend = """
template = template.get("headers")
headers.pop("cookie", None)
cookie = CONFIG.get("MYSHOP_COOKIE") or ""
headers["cookie"] = cookie
body = json.dumps(payload, separators=(",", ":"))
"""
    (wawu / "backend_client.py").write_text(backend, encoding="utf-8")
    subprocess.run(["git", "-C", str(wawu), "add", "backend_client.py"], check=True)
    subprocess.run(
        [
            "git", "-C", str(wawu), "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid", "commit", "-qm", "wawu",
        ],
        check=True,
    )
    env = tmp_path / "shijiu.env"
    env.write_text(
        "MYSHOP_TOKEN=TOKEN_PRIVATE_TEST\n"
        "MYSHOP_SECRET=SECRET_PRIVATE_TEST\n"
        + ("MYSHOP_COOKIE=COOKIE_PRIVATE_TEST\n" if cookie else ""),
        encoding="utf-8",
    )
    template = tmp_path / "native.json"
    payload = {
        "secret": "MASKED",
        "token": "MASKED",
        "good_name": "test",
        "spec_name": [{"spec_name": "规格", "id": 0, "son_name": []}],
        "sku_info": [{"sku_code": "TEST"}],
    }
    template.write_text(
        json.dumps(
            {
                "phase": "native_save",
                "time": "2026-09-04T00:00:00Z",
                "method": "POST",
                "resource_type": "xhr",
                "url": (
                    "https://api.wfcorp.cn/shijiu/shopapi/Goods/"
                    "newAddGood&token=MASKED"
                ),
                "headers": {
                    "accept": "application/json",
                    "content-type": "application/json;charset=UTF-8",
                },
                "post_data": json.dumps(payload),
                "response": {
                    "status": 200,
                    "json": {"code": 200, "msg": "success", "data": []},
                },
            }
        ),
        encoding="utf-8",
    )
    capture = tmp_path / "capture.js"
    capture.write_text(
        "request.allHeaders()" if complete_headers else "request.headers()",
        encoding="utf-8",
    )
    native_result = tmp_path / "native-result.json"
    native_result.write_text(
        json.dumps(
            {
                "status": "PASS_NATIVE_UI_SAVE_VISIBLE",
                "native_save_request": {"present": True},
                "goods_index_verify": {"visible": True},
            }
        ),
        encoding="utf-8",
    )
    direct_loop = tmp_path / "direct-loop.json"
    direct_loop.write_text(
        json.dumps(
            {
                "product_result": {
                    "create_response": {
                        "_native_create_request": {
                            "headers": {
                                "accept": "application/json",
                                "content-type": "application/json;charset=UTF-8",
                            }
                        }
                    }
                },
                "validation": {"passed": True, "backend_sku_count": 3},
                "delete_result": {"post_delete_match_found": False},
            }
        ),
        encoding="utf-8",
    )
    probe = miki / "deliverables/shijiu_import/minimal_create_probe_report.json"
    probe.write_text(
        json.dumps(
            {
                "candidate_product_number": "17-1366-244",
                "state": "STOPPED_ON_PROBE_ERROR",
                "create_response": {
                    "code": 200,
                    "data": [],
                    "_native_request": {
                        "headers": {"content-type": "application/json;charset=UTF-8"},
                        "content_type": "application/json;charset=UTF-8",
                        "serialization": {"format": "JSON"},
                    },
                    "_native_response": {"http_status": 200},
                },
                "requests": {"read": 319, "minimal_create": 1, "staged_update": 0},
                "post_failure_forensics": {"passed": True},
            }
        ),
        encoding="utf-8",
    )
    return env, template, capture, native_result, direct_loop, wawu, probe


def test_missing_cookie_and_incomplete_capture_block_all_writes(tmp_path: Path) -> None:
    inputs = _write_fixture_tree(tmp_path, cookie=False, complete_headers=False)
    report = build_session_audit(
        env_path=inputs[0],
        native_template_path=inputs[1],
        capture_script_path=inputs[2],
        native_result_path=inputs[3],
        direct_loop_result_path=inputs[4],
        wawu_repo=inputs[5],
        mikihouse_probe_report=inputs[6],
        browser_evidence={
            "authenticated_shijiu_admin_visible": False,
            "chrome_extension_connected": False,
        },
    )
    assert report["decision"]["state"] == (
        "BLOCKED_MISSING_BROWSER_EXACT_SESSION_EVIDENCE"
    )
    assert report["decision"]["product_create_requests_executed_this_audit"] == 0
    assert report["decision"]["new_candidate_selected"] is False
    assert report["analysis"]["cookie_absence_in_template_is_conclusive"] is False
    assert report["analysis"][
        "historical_cookie_less_programmatic_create_and_readback_passed"
    ] is True
    serialized = json.dumps(report)
    assert "TOKEN_PRIVATE_TEST" not in serialized
    assert "SECRET_PRIVATE_TEST" not in serialized


def test_complete_private_evidence_only_advances_to_read_only_validation(
    tmp_path: Path,
) -> None:
    inputs = _write_fixture_tree(tmp_path, cookie=True, complete_headers=True)
    report = build_session_audit(
        env_path=inputs[0],
        native_template_path=inputs[1],
        capture_script_path=inputs[2],
        native_result_path=inputs[3],
        direct_loop_result_path=inputs[4],
        wawu_repo=inputs[5],
        mikihouse_probe_report=inputs[6],
        browser_evidence={
            "authenticated_shijiu_admin_visible": True,
            "chrome_extension_connected": True,
        },
    )
    assert report["decision"]["state"] == (
        "READY_FOR_SEPARATE_READ_ONLY_SESSION_VALIDATION"
    )
    assert report["decision"]["next_write_allowed"] is False
    assert report["decision"]["shijiu_read_requests_executed_this_audit"] == 0
    serialized = json.dumps(report)
    assert "COOKIE_PRIVATE_TEST" not in serialized


def test_checked_in_audit_records_no_new_target_requests() -> None:
    report = json.loads(
        (
            ROOT
            / "deliverables/shijiu_import/session_auth_audit.json"
        ).read_text(encoding="utf-8")
    )
    assert report["decision"]["state"] == (
        "BLOCKED_MISSING_BROWSER_EXACT_SESSION_EVIDENCE"
    )
    assert report["decision"]["shijiu_read_requests_executed_this_audit"] == 0
    assert report["decision"]["product_create_requests_executed_this_audit"] == 0
    assert report["decision"]["product_update_requests_executed_this_audit"] == 0
    assert report["decision"]["new_candidate_selected"] is False
    assert report["runtime_config"]["cookie_available"] is False
    assert report["native_capture_implementation"][
        "sensitive_header_capture_complete"
    ] is False
    assert report["report_safety"]["cookie_values_included"] is False
