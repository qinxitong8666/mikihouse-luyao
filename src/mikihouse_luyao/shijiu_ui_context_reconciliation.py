from __future__ import annotations

import copy
import hashlib
import json
import re
import urllib.parse
from pathlib import Path
from typing import Any

from .shijiu_canonical_reconciliation import (
    EXPECTED_BACKEND_SKU_CODE,
    EXPECTED_PRICE_JPY,
    RECONCILIATION_PRODUCT_NUMBER,
)
from .shijiu_import import content_sha256, load_mapping_state, now, write_json_atomic
from .shijiu_live_import import (
    ContractMismatchError,
    DuplicateRiskError,
    LiveImportError,
    persist_verified_mapping,
    validate_product_readback,
)


UI_RECONCILIATION_SCHEMA_VERSION = 1
UI_MODE = "MIKIHOUSE_UI_CONTEXT_STRICT_READ_ONLY_RECONCILIATION"
EXPECTED_NAME = "ヘアゴム（2個セット）"
LIST_PATH = "/shopapi/Goods/index"
DETAIL_PATH = "/shopapi/goods/getFormatInfo"
SENSITIVE_FIELDS = {"token", "secret", "cookie", "authorization"}
MEMBER_PRICE_FIELDS = (
    "first_level",
    "second_level",
    "third_level",
    "fourth_level",
    "fifth_level",
    "sixth_level",
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _latest_private_evidence(private_dir: Path) -> Path:
    matches = sorted(
        private_dir.glob("shijiu-ui-context-reconciliation-*.private.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        raise LiveImportError("no UI-context private reconciliation evidence found")
    return matches[0]


def _row_id(row: dict[str, Any]) -> str:
    return str(row.get("id") or row.get("good_id") or row.get("goods_id") or "").strip()


def _row_name(row: dict[str, Any]) -> str:
    return str(row.get("good_name") or row.get("goods_name") or row.get("name") or "").strip()


def _form(value: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(value, keep_blank_values=True))


def _safe_url_shape(value: str) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(value.replace("&token=", "?token=", 1))
    query = urllib.parse.parse_qs(parsed.query)
    return {
        "scheme": parsed.scheme,
        "host": parsed.netloc,
        "path": parsed.path,
        "query_parameter_names": sorted(query),
        "query_values_included": False,
    }


def _validate_ui_request_context(raw: dict[str, Any], canonical_hash: str) -> dict[str, Any]:
    if raw.get("mode") != UI_MODE:
        raise LiveImportError("private evidence is not a UI-context reconciliation")
    if (
        raw.get("target_name") != EXPECTED_NAME
        or raw.get("target_category_id") != 294884
        or raw.get("expected_backend_sku_code") != EXPECTED_BACKEND_SKU_CODE
    ):
        raise LiveImportError("UI-context reconciliation target drift")
    if raw.get("browser_create_capture_sha256") != canonical_hash:
        raise LiveImportError("UI-context evidence does not reuse the canonical browser CREATE capture")
    safety = raw.get("safety") or {}
    if (
        safety.get("target_mutation_requests_sent") != 0
        or safety.get("blocked_mutation_request_count") not in (0, None)
        or set(safety.get("allowed_paths") or []) != {LIST_PATH, DETAIL_PATH}
    ):
        raise LiveImportError("UI-context evidence failed the zero-mutation boundary")
    request = raw.get("ui_goods_index_request") or {}
    if request.get("method") != "POST" or LIST_PATH not in str(request.get("url") or ""):
        raise LiveImportError("UI-context evidence lacks the real POST Goods.index request")
    base_form = _form(str(request.get("post_data") or ""))
    parsed_url = urllib.parse.urlparse(str(request.get("url") or "").replace("&token=", "?token=", 1))
    query_token = (urllib.parse.parse_qs(parsed_url.query).get("token") or [""])[0]
    body_token = base_form.get("token") or ""
    if not query_token or not base_form.get("secret") or (body_token and query_token != body_token):
        raise LiveImportError("UI Goods.index query/body auth context is inconsistent")
    expected_labels = {"category_294884", "all_categories"}
    queries = raw.get("queries") or []
    if {row.get("label") for row in queries} != expected_labels:
        raise LiveImportError("both category and unscoped UI queries are required")
    safe_base = {key: value for key, value in base_form.items() if key not in SENSITIVE_FIELDS}
    query_summaries = []
    for query in queries:
        fields = {str(key): str(value) for key, value in (query.get("request_form") or {}).items()}
        label = query["label"]
        if fields.get("good_name") != EXPECTED_NAME:
            raise LiveImportError("UI query good_name is not exact")
        expected_type = "294884" if label == "category_294884" else ""
        if fields.get("good_type") != expected_type:
            raise LiveImportError("UI query category scope mismatch")
        if fields.get("token") != base_form.get("token") or fields.get("secret") != base_form.get("secret"):
            raise LiveImportError("UI query did not preserve token/secret")
        for key, value in base_form.items():
            if key not in {"good_name", "good_type", "page"} and fields.get(key) != value:
                raise LiveImportError(f"UI query changed unrelated form field: {key}")
        changed = set(query.get("changed_fields_from_ui_request") or [])
        if not changed <= {"good_name", "good_type", "page"} or "good_name" not in changed:
            raise LiveImportError("UI query changed fields beyond name/category/page")
        exact_rows = query.get("exact_rows") or []
        if any(_row_name(row) != EXPECTED_NAME or not _row_id(row) for row in exact_rows):
            raise LiveImportError("UI query evidence contains a non-exact candidate")
        query_summaries.append({
            "label": label,
            "good_type": expected_type,
            "changed_fields_from_ui_request": sorted(changed),
            "declared_count": query.get("declared_count"),
            "pages_read": query.get("pages_read"),
            "exact_match_product_ids": sorted({_row_id(row) for row in exact_rows}),
        })
    headers = {str(key).lower(): str(value) for key, value in (request.get("headers") or {}).items()}
    return {
        "request": request,
        "base_form": base_form,
        "summary": {
            "method": "POST",
            "endpoint": _safe_url_shape(str(request["url"])),
            "header_names": sorted(headers),
            "cookie_present": bool(headers.get("cookie")),
            "authorization_present": bool(headers.get("authorization")),
            "content_type": headers.get("content-type"),
            "form_field_names_in_order": list(base_form),
            "filter_context": safe_base,
            "auth_context": {
                "query_token_present": True,
                "body_token_present": bool(body_token),
                "body_secret_present": True,
                "query_body_token_equal": query_token == body_token if body_token else None,
                "values_included": False,
            },
            "queries": sorted(query_summaries, key=lambda row: row["label"]),
            "sensitive_values_included": False,
        },
    }


def _text_summary(value: Any, *, preview_limit: int = 120) -> dict[str, Any]:
    text = str(value or "")
    return {
        "empty": not text,
        "length": len(text),
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "preview": text if len(text) <= preview_limit else text[:preview_limit] + "…",
    }


def _url_summary(value: Any) -> dict[str, Any]:
    text = str(value or "")
    urls = re.findall(r"https?://[^\s,'\"<>]+", text)
    hosts = sorted({urllib.parse.urlparse(url).netloc for url in urls})
    return {
        "empty": not text,
        "url_count": len(urls),
        "hosts": hosts,
        "comma_separated": bool("," in text),
        "value_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "raw_urls_included": False,
    }


def _spec_summary(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for row in value:
        if not isinstance(row, dict):
            continue
        result.append({
            "spec_name": row.get("spec_name"),
            "id": row.get("id"),
            "son_name": [
                {"spec_name": child.get("spec_name"), "id": child.get("id")}
                for child in row.get("son_name") or []
                if isinstance(child, dict)
            ],
        })
    return result


def _sku_summary(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    fields = (
        "sku_code",
        "sku_price",
        "sku_cost_price",
        "sku_stock",
        "spec_name",
        *MEMBER_PRICE_FIELDS,
    )
    result = []
    for row in value:
        if not isinstance(row, dict):
            continue
        summary = {key: row.get(key) for key in fields}
        summary["sku_thumbnail"] = _url_summary(row.get("sku_thumbnail"))
        result.append(summary)
    return result


def business_value_difference(browser: dict[str, Any], miki: dict[str, Any]) -> dict[str, Any]:
    scalar_fields = ("good_type", "good_name", "supplier", "cargo_place", "bus_region")
    scalars = [
        {
            "field": field,
            "browser_success_value": browser.get(field),
            "canonical_mikihouse_value": miki.get(field),
            "equal": browser.get(field) == miki.get(field),
        }
        for field in scalar_fields
    ]
    text_fields = ("good_describe", "description", "good_details")
    text = [
        {
            "field": field,
            "browser_success": _text_summary(browser.get(field)),
            "canonical_mikihouse": _text_summary(miki.get(field)),
            "equal": str(browser.get(field) or "") == str(miki.get(field) or ""),
        }
        for field in text_fields
    ]
    images = [
        {
            "field": field,
            "browser_success": _url_summary(browser.get(field)),
            "canonical_mikihouse": _url_summary(miki.get(field)),
            "equal": str(browser.get(field) or "") == str(miki.get(field) or ""),
        }
        for field in ("master_graph", "broadcast", "good_detail_pics")
    ]
    browser_specs = _spec_summary(browser.get("spec_name"))
    miki_specs = _spec_summary(miki.get("spec_name"))
    browser_skus = _sku_summary(browser.get("sku_info"))
    miki_skus = _sku_summary(miki.get("sku_info"))
    return {
        "scope": "business values only; auth values, raw headers and raw image URLs excluded",
        "scalar_values": scalars,
        "text_values": text,
        "spec_name_and_son_name": {
            "browser_success": browser_specs,
            "canonical_mikihouse": miki_specs,
            "equal": browser_specs == miki_specs,
        },
        "sku_values": {
            "browser_success": browser_skus,
            "canonical_mikihouse": miki_skus,
            "equal": browser_skus == miki_skus,
        },
        "image_values": images,
        "sensitive_values_included": False,
    }


def finalize_ui_context_reconciliation(
    private_dir: Path,
    item: dict[str, Any],
    payload: dict[str, Any],
    checkpoint: dict[str, Any],
    special: set[str],
    canonical_contract: dict[str, Any],
    *,
    checkpoint_path: Path,
    mapping_path: Path,
    validation_report_path: Path,
    candidate_report_path: Path,
    report_path: Path,
) -> dict[str, Any]:
    if item.get("product_number") != RECONCILIATION_PRODUCT_NUMBER:
        raise LiveImportError("UI reconciliation is hard-limited to 36-2001-572")
    if item["product_number"] in special or len(special) != 351:
        raise LiveImportError("PDF_SPECIAL_LIST boundary failed")
    if checkpoint.get("create_attempts") != 1:
        raise DuplicateRiskError("UI reconciliation requires exactly one historical CREATE")
    mapping = load_mapping_state(mapping_path)
    mapping_row = mapping["products"][RECONCILIATION_PRODUCT_NUMBER]
    if mapping_row.get("shijiu_product_id") not in (None, ""):
        if (
            checkpoint.get("status") == "RECONCILED_READBACK_VERIFIED_UI_CONTEXT"
            and str(checkpoint.get("shijiu_product_id")) == str(mapping_row["shijiu_product_id"])
            and report_path.exists()
        ):
            return json.loads(report_path.read_text(encoding="utf-8"))
        raise DuplicateRiskError("UI reconciliation refuses to replace an existing mapping")

    private_path = _latest_private_evidence(private_dir)
    private_bytes = private_path.read_bytes()
    raw = json.loads(private_bytes)
    context = _validate_ui_request_context(
        raw, str(canonical_contract["browser_exact_private_evidence_sha256"])
    )
    candidate_rows: dict[str, dict[str, Any]] = {}
    for query in raw.get("queries") or []:
        for row in query.get("exact_rows") or []:
            candidate_rows[_row_id(row)] = row
    detail_by_id = {
        str(row.get("product_id")): row
        for row in raw.get("details") or []
        if row.get("product_id")
    }
    if set(detail_by_id) != set(candidate_rows):
        raise LiveImportError("every UI candidate must have a same-context getFormatInfo response")
    verified = []
    validations = []
    for product_id, row in candidate_rows.items():
        detail_record = detail_by_id[product_id]
        detail_response = detail_record.get("response") or {}
        detail_json = copy.deepcopy(detail_response.get("json") or {})
        original_success_contract = {
            "code": detail_json.get("code"),
            "msg": detail_json.get("msg"),
        }
        # The real browser-context endpoint wraps successful getFormatInfo with
        # code=200/msg=success, while the previously audited direct client saw
        # code=1. Normalize only this evidenced UI wrapper before applying the
        # unchanged full product/SKU/image validator.
        if str(detail_json.get("code")) == "200" and str(detail_json.get("msg") or "").casefold() == "success":
            detail_json["code"] = 1
        observation = {
            "product_id": product_id,
            "passed": False,
            "ui_detail_success_contract": original_success_contract,
        }
        try:
            readback = validate_product_readback(
                item,
                payload,
                product_id,
                detail_json,
                create_response=checkpoint.get("create_response"),
                list_row=row,
                require_is_shelf=False,
            )
        except ContractMismatchError as error:
            observation["mismatch"] = str(error)
        else:
            for sku in readback["skus"]:
                sku["shijiu_sku_id"] = None
            verified.append(readback)
            observation.update({
                "passed": True,
                "backend_sku_code": EXPECTED_BACKEND_SKU_CODE,
                "price_jpy": EXPECTED_PRICE_JPY,
                "category_id": 294884,
                "specification_verified": True,
                "images_verified": True,
                "is_shelf_exposed": readback["is_shelf_exposed"],
                "is_shelf_policy": "missing accepted only for UI-context; explicit nonzero rejected",
            })
        validations.append(observation)

    if len(verified) == 1:
        status = "RECONCILED_READBACK_VERIFIED_UI_CONTEXT"
        readback = verified[0]
        persist_verified_mapping(mapping_path, item, readback, content_sha256(payload))
    elif not candidate_rows:
        status = "HISTORICAL_CREATE_NOT_PERSISTED_CONFIRMED_BY_UI_CONTEXT"
        readback = None
    else:
        status = "UI_CONTEXT_NO_UNIQUE_STRONG_EVIDENCE"
        readback = None

    value_diff = business_value_difference(
        raw.get("browser_create_business_payload") or {}, payload
    )
    safe_request_count = int((raw.get("safety") or {}).get("read_only_request_count") or 0)
    private_evidence_sha256 = _sha256_bytes(private_bytes)
    report = {
        "schema_version": UI_RECONCILIATION_SCHEMA_VERSION,
        "generated_at": now(),
        "mode": UI_MODE,
        "status": status,
        "source": "MIKIHOUSE",
        "target": "SHIJIU",
        "product_number": RECONCILIATION_PRODUCT_NUMBER,
        "exact_good_name": EXPECTED_NAME,
        "target_category_id": 294884,
        "expected_backend_sku_code": EXPECTED_BACKEND_SKU_CODE,
        "expected_price_jpy": EXPECTED_PRICE_JPY,
        "currency": "JPY",
        "historical_create_attempts": 1,
        "ui_context": context["summary"],
        "private_evidence_sha256": private_evidence_sha256,
        "private_evidence_path_included": False,
        "canonical_browser_create_evidence_sha256": raw["browser_create_capture_sha256"],
        "candidate_product_ids": sorted(candidate_rows),
        "candidate_validations": validations,
        "verified_product_ids": [row["shijiu_product_id"] for row in verified],
        "mapping_persisted": bool(readback),
        "shijiu_product_id": readback["shijiu_product_id"] if readback else None,
        "shijiu_sku_id": None,
        "not_persisted_confirmed": status == "HISTORICAL_CREATE_NOT_PERSISTED_CONFIRMED_BY_UI_CONTEXT",
        "business_value_difference": value_diff,
        "diagnostic_conclusion": {
            "historical_create_persisted": bool(readback),
            "prior_direct_client_result_was_false_negative": bool(readback),
            "ui_context_difference": (
                "real Goods.index uses its captured endpoint/auth/form/filter context; "
                "notably the captured safe filter values are preserved in ui_context.filter_context"
            ),
            "business_value_differences_are_causal": "NOT_PROVEN",
            "additional_write_authorized": False,
        },
        "safety": {
            "read_only_requests": safe_request_count,
            "create_requests": 0,
            "image_upload_requests": 0,
            "update_requests": 0,
            "other_target_mutations": 0,
            "legacy_product_mutations": 0,
            "additional_mikihouse_products_touched": 0,
            "pdf_special_exclusion_count": len(special),
        },
        "sensitive_values_included": False,
    }

    ledger = []
    for query in raw.get("queries") or []:
        for _ in query.get("responses") or []:
            ledger.append({"path": LIST_PATH, "semantic_operation": "read", "operation": query["label"]})
    for detail in raw.get("details") or []:
        ledger.append({
            "path": DETAIL_PATH,
            "semantic_operation": "read",
            "operation": "UI-context candidate strong verification",
            "product_id": str(detail.get("product_id")),
        })
    if len(ledger) != safe_request_count:
        raise LiveImportError("private evidence request count and sanitized ledger disagree")
    previously_recorded = False
    if report_path.exists() and checkpoint.get("ui_context_reconciliation"):
        previous_report = json.loads(report_path.read_text(encoding="utf-8"))
        previously_recorded = (
            previous_report.get("private_evidence_sha256") == private_evidence_sha256
        )
    if not previously_recorded:
        base_sequence = len(checkpoint.get("request_ledger") or [])
        for index, row in enumerate(ledger, start=1):
            checkpoint.setdefault("request_ledger", []).append({
                "sequence": base_sequence + index,
                "at": report["generated_at"],
                "method": "POST",
                **row,
            })
    previous_error = copy.deepcopy(checkpoint.get("error"))
    current_error = None if readback else ({
        "type": "HistoricalCreateNotPersisted",
        "message": "browser UI-context exact-name queries found no product in category 294884 or all categories",
        "at": report["generated_at"],
    } if report["not_persisted_confirmed"] else {
        "type": "UiContextNoUniqueStrongEvidence",
        "message": "UI-context candidates did not produce exactly one full readback match",
        "at": report["generated_at"],
    })
    checkpoint.update({
        "updated_at": now(),
        "status": status,
        "shijiu_product_id": report["shijiu_product_id"],
        "readback": readback,
        "mapping_persisted": bool(readback),
        "prior_ui_reconciliation_error": previous_error,
        "error": current_error,
        "ui_context_reconciliation": {
            "status": status,
            "at": report["generated_at"],
            "report": "canonical_create_ui_context_reconciliation_report.json",
            "private_evidence_sha256": private_evidence_sha256,
            "candidate_product_ids": report["candidate_product_ids"],
            "verified_product_ids": report["verified_product_ids"],
            "target_mutations": 0,
        },
    })
    write_json_atomic(checkpoint_path, checkpoint)
    write_json_atomic(report_path, report)

    validation_report = json.loads(validation_report_path.read_text(encoding="utf-8"))
    validation_report.update({
        "generated_at": now(),
        "status": status,
        "shijiu_product_id": report["shijiu_product_id"],
        "mapping_persisted": bool(readback),
        "exact_backend_sku_match_count": 1 if readback else 0,
        "verified_variants": readback["skus"] if readback else [],
        "read_request_count": len(checkpoint["request_ledger"]),
        "error": current_error,
        "ui_context_reconciliation": {
            "report": report_path.name,
            "status": status,
            "target_mutations": 0,
        },
    })
    write_json_atomic(validation_report_path, validation_report)
    candidate_report = json.loads(candidate_report_path.read_text(encoding="utf-8"))
    candidate_report.update({
        "result": status,
        "shijiu_product_id": report["shijiu_product_id"],
        "ui_context_reconciliation_read_only": True,
        "additional_write_executed": False,
    })
    write_json_atomic(candidate_report_path, candidate_report)
    return report
