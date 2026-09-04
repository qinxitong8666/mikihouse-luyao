from __future__ import annotations

import hashlib
import json
import re
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any

from .shijiu_import import load_env_file, now, write_json_atomic


AUDIT_SCHEMA_VERSION = 1
SENSITIVE_ENV_KEYS = (
    "SHIJIU_TOKEN",
    "SHIJIU_SECRET",
    "SHIJIU_COOKIE",
    "MYSHOP_TOKEN",
    "MYSHOP_SECRET",
    "MYSHOP_COOKIE",
)


class SessionAuditError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_url_shape(value: str) -> dict[str, Any]:
    text = str(value or "")
    parsed = urllib.parse.urlparse(text)
    path = parsed.path.split("&", 1)[0]
    parameter_names = sorted(
        set(re.findall(r"(?:[?&])([^=&?#]+)=", text))
    )
    return {
        "scheme": parsed.scheme,
        "host": parsed.netloc,
        "path": path,
        "query_parameter_names": parameter_names,
        "contains_query_values": bool(parameter_names),
    }


def _value_shape(value: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"type": type(value).__name__}
    if isinstance(value, (list, dict, str)):
        result["length"] = len(value)
    return result


def _git_head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def inspect_runtime_config(path: Path) -> tuple[dict[str, Any], dict[str, str]]:
    values = load_env_file(path)
    cookie_key = next(
        (key for key in ("SHIJIU_COOKIE", "MYSHOP_COOKIE") if values.get(key)),
        None,
    )
    safe = {
        "location_scope": "external_local_non_git_config",
        "exists": path.is_file(),
        "auth_key_presence": {
            key: bool(values.get(key)) for key in SENSITIVE_ENV_KEYS
        },
        "token_available": bool(values.get("SHIJIU_TOKEN") or values.get("MYSHOP_TOKEN")),
        "secret_available": bool(
            values.get("SHIJIU_SECRET") or values.get("MYSHOP_SECRET")
        ),
        "cookie_available": cookie_key is not None,
        "cookie_source_key": cookie_key,
        "native_request_path_configured": bool(
            values.get("NATIVE_SAVE_REQUEST_PATH")
        ),
    }
    return safe, values


def inspect_capture_implementation(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    uses_basic_headers = bool(re.search(r"request\.headers\s*\(\s*\)", text))
    uses_all_headers = bool(re.search(r"request\.allHeaders\s*\(\s*\)", text))
    uses_extra_info = "requestWillBeSentExtraInfo" in text
    complete = uses_all_headers or uses_extra_info
    return {
        "location_scope": "external_local_non_git_capture_implementation",
        "exists": path.is_file(),
        "sha256": _sha256(path) if path.is_file() else None,
        "uses_request_headers": uses_basic_headers,
        "uses_request_all_headers": uses_all_headers,
        "uses_cdp_request_will_be_sent_extra_info": uses_extra_info,
        "sensitive_header_capture_complete": complete,
        "limitation": (
            None
            if complete
            else "request.headers() cannot prove whether protected Cookie headers were sent"
        ),
    }


def inspect_native_template(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "location_scope": "external_local_non_git_native_template",
            "exists": False,
        }
    raw = json.loads(path.read_text(encoding="utf-8"))
    headers = {
        str(key).casefold(): value for key, value in (raw.get("headers") or {}).items()
    }
    body_text = raw.get("post_data") or ""
    try:
        body = json.loads(body_text)
    except (TypeError, json.JSONDecodeError):
        body = None
    response = raw.get("response") or {}
    response_json = response.get("json") or {}
    return {
        "location_scope": "external_local_non_git_native_template",
        "exists": True,
        "sha256": _sha256(path),
        "byte_count": path.stat().st_size,
        "phase": raw.get("phase"),
        "captured_at": raw.get("time"),
        "method": raw.get("method"),
        "resource_type": raw.get("resource_type"),
        "url_shape": _safe_url_shape(raw.get("url") or ""),
        "header_names": sorted(headers),
        "cookie_header_observed": "cookie" in headers,
        "content_type": headers.get("content-type") or raw.get("content_type"),
        "body": {
            "type": "json_object" if isinstance(body, dict) else "unparsed",
            "field_names_in_order": list(body) if isinstance(body, dict) else [],
            "field_count": len(body) if isinstance(body, dict) else 0,
            "has_secret_field": isinstance(body, dict) and "secret" in body,
            "has_token_field": isinstance(body, dict) and "token" in body,
            "sku_count": len(body.get("sku_info") or []) if isinstance(body, dict) else 0,
            "specification_count": (
                len(body.get("spec_name") or []) if isinstance(body, dict) else 0
            ),
        },
        "response": {
            "http_status": response.get("status"),
            "api_code": response_json.get("code"),
            "api_message_shape": _value_shape(response_json.get("msg")),
            "api_data_shape": _value_shape(response_json.get("data")),
        },
    }


def inspect_native_result(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        "exists": True,
        "sha256": _sha256(path),
        "status": raw.get("status"),
        "native_save_captured": bool(raw.get("native_save_request")),
        "goods_index_visible": bool(
            (raw.get("goods_index_verify") or {}).get("visible")
        ),
        "custom_fetch_capture_present": bool(raw.get("custom_fetch_request")),
    }


def inspect_direct_loop_result(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False}
    raw = json.loads(path.read_text(encoding="utf-8"))
    product = raw.get("product_result") or {}
    create = product.get("create_response") or {}
    request = create.get("_native_create_request") or {}
    headers = {
        str(key).casefold(): value
        for key, value in (request.get("headers") or {}).items()
    }
    validation = raw.get("validation") or product.get("validation") or {}
    return {
        "exists": True,
        "sha256": _sha256(path),
        "validation_passed": bool(validation.get("passed")),
        "validated_sku_count": validation.get("backend_sku_count"),
        "native_request_header_names": sorted(headers),
        "cookie_header_sent": "cookie" in headers,
        "product_create_and_readback_proven": bool(validation.get("passed")),
        "post_test_cleanup_proven": (
            (raw.get("delete_result") or {}).get("post_delete_match_found") is False
        ),
    }


def inspect_wawu_writer(repo: Path) -> dict[str, Any]:
    path = repo / "backend_client.py"
    text = path.read_text(encoding="utf-8")
    return {
        "repository": "qinxitong8666/wawu-product-sync",
        "main_commit": _git_head(repo),
        "backend_client_sha256": _sha256(path),
        "target_endpoint": "/shopapi/Goods/newAddGood",
        "native_request_path_config_key": "NATIVE_SAVE_REQUEST_PATH",
        "default_native_template_location": (
            "external outputs/browser_session_reports/native_save_request.json"
        ),
        "template_headers_loaded": "template.get(\"headers\")" in text,
        "template_cookie_removed_before_send": 'headers.pop("cookie", None)' in text,
        "runtime_cookie_config_key": "MYSHOP_COOKIE",
        "runtime_cookie_injected_when_present": (
            'headers["cookie"] = cookie' in text
        ),
        "compact_json_body": 'separators=(",", ":")' in text,
        "body_auth_fields_first": ["secret", "token"],
        "native_request_template_is_committed": False,
    }


def inspect_mikihouse_probe(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    request = (raw.get("create_response") or {}).get("_native_request") or {}
    headers = {str(key).casefold(): value for key, value in (request.get("headers") or {}).items()}
    return {
        "report_sha256": _sha256(path),
        "candidate_product_number": raw.get("candidate_product_number"),
        "state": raw.get("state"),
        "request_header_names": sorted(headers),
        "cookie_header_sent": "cookie" in headers,
        "content_type": request.get("content_type"),
        "serialization": request.get("serialization"),
        "api_response": {
            "http_status": ((raw.get("create_response") or {}).get("_native_response") or {}).get("http_status"),
            "code": (raw.get("create_response") or {}).get("code"),
            "data_shape": _value_shape((raw.get("create_response") or {}).get("data")),
        },
        "read_only_requests": (raw.get("requests") or {}).get("read"),
        "post_failure_read_only_passed": bool(
            (raw.get("post_failure_forensics") or {}).get("passed")
        ),
        "create_requests": (raw.get("requests") or {}).get("minimal_create"),
        "staged_updates": (raw.get("requests") or {}).get("staged_update"),
    }


def _assert_no_sensitive_values(report: dict[str, Any], values: dict[str, str]) -> None:
    serialized = json.dumps(report, ensure_ascii=False)
    for key in SENSITIVE_ENV_KEYS:
        value = values.get(key) or ""
        if len(value) >= 4 and value in serialized:
            raise SessionAuditError(f"sensitive value for {key} leaked into audit report")


def build_session_audit(
    *,
    env_path: Path,
    native_template_path: Path,
    capture_script_path: Path,
    native_result_path: Path,
    direct_loop_result_path: Path,
    wawu_repo: Path,
    mikihouse_probe_report: Path,
    browser_evidence: dict[str, Any],
) -> dict[str, Any]:
    runtime, sensitive_values = inspect_runtime_config(env_path)
    capture = inspect_capture_implementation(capture_script_path)
    template = inspect_native_template(native_template_path)
    native_result = inspect_native_result(native_result_path)
    direct_loop_result = inspect_direct_loop_result(direct_loop_result_path)
    wawu = inspect_wawu_writer(wawu_repo)
    mikihouse = inspect_mikihouse_probe(mikihouse_probe_report)
    template_exact = bool(
        template.get("exists")
        and template.get("phase") == "native_save"
        and template.get("method") == "POST"
        and template.get("url_shape", {}).get("path")
        == "/shijiu/shopapi/Goods/newAddGood"
        and template.get("content_type") == "application/json;charset=UTF-8"
        and template.get("body", {}).get("has_secret_field")
        and template.get("body", {}).get("has_token_field")
    )
    authenticated_browser_visible = bool(
        browser_evidence.get("authenticated_shijiu_admin_visible")
    )
    cookie_requirement_resolved = bool(
        runtime["cookie_available"]
        or (
            capture["sensitive_header_capture_complete"]
            and not template.get("cookie_header_observed")
        )
    )
    session_evidence_complete = bool(
        cookie_requirement_resolved
        and capture["sensitive_header_capture_complete"]
        and authenticated_browser_visible
        and template_exact
    )
    decision = (
        "READY_FOR_SEPARATE_READ_ONLY_SESSION_VALIDATION"
        if session_evidence_complete
        else "BLOCKED_MISSING_BROWSER_EXACT_SESSION_EVIDENCE"
    )
    missing = []
    if not runtime["cookie_available"]:
        missing.append(
            {
                "id": "CURRENT_LOGIN_COOKIE",
                "required_private_content": (
                    "the complete Cookie request-header value, if present on a successful "
                    "logged-in browser newAddGood request"
                ),
                "store_only_in": (
                    "external .secrets/shijiu.env as SHIJIU_COOKIE or MYSHOP_COOKIE"
                ),
            }
        )
    if not capture["sensitive_header_capture_complete"]:
        missing.append(
            {
                "id": "COMPLETE_BROWSER_REQUEST_HEADERS",
                "required_private_content": (
                    "Copy as cURL/HAR headers or Playwright request.allHeaders()/CDP "
                    "requestWillBeSentExtraInfo from a successful native browser save"
                ),
                "must_include": (
                    "method, exact endpoint/query style, every request-header name/value "
                    "including Cookie when sent, Content-Type, and User-Agent/client hints"
                ),
            }
        )
    if not authenticated_browser_visible:
        missing.append(
            {
                "id": "CURRENT_AUTHENTICATED_BROWSER_SESSION",
                "required_private_content": (
                    "a currently logged-in Shijiu admin tab with MikiHouse category access"
                ),
            }
        )
    missing.append(
        {
            "id": "SUCCESSFUL_NATIVE_SAVE_AND_READBACK_PAIR",
            "required_private_content": (
                "one browser-native new-product save capture plus the resulting unique "
                "Goods/index product ID and getFormatInfo SKU ID"
            ),
            "purpose": (
                "prove the exact private template is currently authorized and durable "
                "before any MIKIHOUSE write"
            ),
        }
    )
    report = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "generated_at": now(),
        "mode": "LOCAL_AND_READ_ONLY_EVIDENCE_AUDIT",
        "source": "MIKIHOUSE",
        "target": "SHIJIU",
        "repositories": {
            "mikihouse_main_commit": _git_head(
                mikihouse_probe_report.resolve().parents[2]
            ),
            "wawu_reference": wawu,
        },
        "runtime_config": runtime,
        "native_browser_template": template,
        "native_capture_implementation": capture,
        "native_historical_result": native_result,
        "historical_programmatic_direct_loop": direct_loop_result,
        "mikihouse_previous_probe": mikihouse,
        "browser_environment": browser_evidence,
        "analysis": {
            "template_shape_matches_native_save": template_exact,
            "historical_native_save_was_visible": native_result.get("goods_index_visible"),
            "cookie_absence_in_template_is_conclusive": capture[
                "sensitive_header_capture_complete"
            ],
            "reason_cookie_absence_is_not_conclusive": (
                "the capture used request.headers(), not allHeaders() or CDP extra-info"
            ),
            "wawu_programmatic_writer_would_send_cookie_now": runtime["cookie_available"],
            "cookie_requirement_resolved": cookie_requirement_resolved,
            "historical_cookie_less_programmatic_create_and_readback_passed": bool(
                direct_loop_result.get("product_create_and_readback_proven")
                and not direct_loop_result.get("cookie_header_sent")
            ),
            "mikihouse_previous_create_sent_cookie": mikihouse["cookie_header_sent"],
            "current_token_secret_support_read_only_access": bool(
                mikihouse["post_failure_read_only_passed"]
                and mikihouse["read_only_requests"]
            ),
            "current_browser_exact_session_evidence_complete": session_evidence_complete,
            "cookie_is_proven_root_cause": False,
            "conclusion": (
                "No current browser-exact authenticated save request can be reproduced. "
                "A historical programmatic create/readback succeeded without Cookie, so "
                "Cookie absence alone is not a sufficient explanation. The browser capture "
                "also cannot prove whether the native UI request carried protected headers."
            ),
        },
        "decision": {
            "state": decision,
            "shijiu_read_requests_executed_this_audit": 0,
            "image_upload_requests_executed_this_audit": 0,
            "product_create_requests_executed_this_audit": 0,
            "product_update_requests_executed_this_audit": 0,
            "new_candidate_selected": False,
            "different_mikihouse_product_create_attempted": False,
            "legacy_products_touched": 0,
            "next_write_allowed": False,
        },
        "minimum_required_private_evidence": missing,
        "report_safety": {
            "token_values_included": False,
            "secret_values_included": False,
            "cookie_values_included": False,
            "native_request_body_values_included": False,
            "external_private_files_copied_into_git": False,
        },
    }
    _assert_no_sensitive_values(report, sensitive_values)
    return report


def write_session_audit(path: Path, report: dict[str, Any]) -> None:
    write_json_atomic(path, report)
