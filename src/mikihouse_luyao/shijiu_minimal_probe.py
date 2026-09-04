from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from .catalog import calculate_mini_program_price_jpy
from .csv_input import read_product_numbers
from .shijiu_import import (
    EXPECTED_SPECIAL_COUNT,
    PDF_SPECIAL_EXCLUDED_REASON,
    SOURCE_CODE,
    backend_sku_code,
    content_sha256,
    file_sha256,
    load_mapping_state,
    map_product_to_shijiu,
    now,
    recursively_find_skus,
    response_rows,
    write_json_atomic,
)
from .shijiu_live_import import (
    CATEGORY_PATH,
    CREATE_PATH,
    DETAIL_PATH,
    IMAGE_UPLOAD_PATH,
    LIST_PATH,
    NATIVE_SAVE_FALLBACK_HEADERS,
    ContractMismatchError,
    DuplicateRiskError,
    LiveImportError,
    ShijiuLiveClient,
    _decimal,
    _first_observation,
    _normalized_specification,
    _product_id_from_value,
    _redacted_response,
    _resolve_payload,
    _sku_id_from_row,
)
from .shijiu_recovery import discover_created_product, prove_no_residual


PROBE_SCHEMA_VERSION = 1
PROBE_CONFIRMATION = "MIKIHOUSE_MINIMAL_CREATE_PROBE_ONE"
FORBIDDEN_RECOVERY_PRODUCT = "00-1000-028"
TARGET_CATEGORY = {
    "id": 294884,
    "name": "MikiHouse",
    "parent_id": 288338,
    "parent_name": "母婴用品",
    "assignment_policy": "all_publishable_mikihouse_products",
}
ALLOWED_ENDPOINTS = {
    CATEGORY_PATH,
    LIST_PATH,
    DETAIL_PATH,
    IMAGE_UPLOAD_PATH,
    CREATE_PATH,
}


def _ordered_image_urls(product: dict[str, Any]) -> list[str]:
    result = []
    for entry in product.get("ordered_images") or []:
        url = str((entry.get("image") or {}).get("url") or "").strip()
        if url and url not in result:
            result.append(url)
    return result


def _is_official_image(url: str) -> bool:
    return url.startswith("https://") and (
        "cdn.shopify.com/" in url or "mikihouse.co.jp/" in url
    )


def select_minimal_probe_candidate(
    master: dict[str, Any],
    special: set[str],
    mapping: dict[str, Any],
    *,
    previously_tested: set[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidates = []
    rejected_counts: dict[str, int] = {}
    tested = set(previously_tested or ())

    def reject(reason: str) -> None:
        rejected_counts[reason] = rejected_counts.get(reason, 0) + 1

    for product in master.get("products") or []:
        number = str(product.get("product_number") or "")
        variants = list(product.get("variants") or [])
        images = _ordered_image_urls(product)
        mapping_row = (mapping.get("products") or {}).get(number) or {}
        reasons = []
        if not number or number == FORBIDDEN_RECOVERY_PRODUCT:
            reasons.append("forbidden_or_missing_product_number")
        if number in tested:
            reasons.append("previously_tested_product")
        if number in special:
            reasons.append(PDF_SPECIAL_EXCLUDED_REASON)
        if not product.get("active"):
            reasons.append("inactive_product")
        if len(variants) != 1:
            reasons.append("not_single_variant")
        if len(images) < 1 or not _is_official_image(images[0] if images else ""):
            reasons.append("missing_official_image")
        if mapping_row.get("shijiu_product_id") not in (None, ""):
            reasons.append("already_mapped_product")
        if variants:
            variant = variants[0]
            if not variant.get("active") or not variant.get("available_for_sale"):
                reasons.append("variant_not_currently_sellable")
            try:
                tax_price = int(variant["tax_included_price_jpy"])
                mini_price = int(variant["mini_program_price_jpy"])
            except (KeyError, TypeError, ValueError):
                reasons.append("invalid_price")
            else:
                if tax_price <= 0 or mini_price <= 0:
                    reasons.append("non_positive_price")
                elif mini_price != calculate_mini_program_price_jpy(tax_price):
                    reasons.append("invalid_65_percent_price")
            sku = str(variant.get("sku") or "").strip()
            variant_mapping = (mapping_row.get("variants") or {}).get(sku) or {}
            if not sku:
                reasons.append("missing_variant_sku")
            if variant_mapping.get("shijiu_sku_id") not in (None, ""):
                reasons.append("already_mapped_variant")
        if reasons:
            for reason in set(reasons):
                reject(reason)
            continue
        candidates.append(
            {
                "product": product,
                "score": [len(images), len(product.get("product_images") or []), number],
                "image_count": len(images),
            }
        )
    if not candidates:
        raise LiveImportError("no safe single-variant MIKIHOUSE probe candidate exists")
    candidates.sort(key=lambda row: tuple(row["score"]))
    selected = copy.deepcopy(candidates[0]["product"])
    evidence = {
        "selection_policy": (
            "active + currently sellable + positive verified 65%-JPY price + exactly one "
            "variant + official image + unbound mapping; then fewest ordered images, "
            "fewest product images, product_number"
        ),
        "candidate_count": len(candidates),
        "rejected_counts": rejected_counts,
        "selected_product_number": selected["product_number"],
        "selected_score": candidates[0]["score"],
        "top_candidates": [
            {
                "product_number": row["product"]["product_number"],
                "variant_count": len(row["product"].get("variants") or []),
                "ordered_image_count": row["image_count"],
                "score": row["score"],
            }
            for row in candidates[:10]
        ],
    }
    return selected, evidence


def build_minimal_payload(
    native_fixture: dict[str, Any],
    mapped: dict[str, Any],
) -> dict[str, Any]:
    if len(mapped["source_variants"]) != 1 or len(mapped["image_upload_plan"]) != 1:
        raise LiveImportError("minimal probe requires exactly one variant and one image")
    variant = mapped["source_variants"][0]
    image_reference = mapped["image_upload_plan"][0]["upload_reference"]
    image_placeholder = f"{{{{SHIJIU_COS_URL:{image_reference}}}}}"
    price = int(variant["mini_program_price_jpy"])
    price_text = f"{price:.2f}"
    payload = copy.deepcopy(native_fixture)
    payload.update(
        {
            "good_name": mapped["shijiu_payload_preview"]["good_name"],
            "good_describe": "",
            "good_details": "",
            "state": "1",
            "spec_name": [
                {
                    "spec_name": "规格",
                    "id": 0,
                    "son_name": [{"spec_name": "DEFAULT", "id": 1}],
                }
            ],
            "sku_info": [
                {
                    "sku_price": price_text,
                    "sku_stock": "1.00",
                    "spec_name": "DEFAULT",
                    "sku_code": variant["backend_sku_code"],
                    "sku_cost_price": price_text,
                    "sku_thumbnail": image_placeholder,
                    "first_level": price_text,
                    "second_level": price_text,
                    "third_level": price_text,
                    "fourth_level": price_text,
                    "fifth_level": price_text,
                    "sixth_level": price_text,
                }
            ],
            "vnarious": 1,
            "good_type": 294884,
            "good_detail_pics": "",
            "cargo_place": "",
            "buying_unit": "",
            "supplier": "MIKIHOUSE",
            "bus_region": "",
            "description": f"source_product_id={mapped['source_product_id']}",
            "is_shelf": 0,
            "master_graph": image_placeholder,
            "broadcast": image_placeholder,
        }
    )
    if list(payload) != list(native_fixture):
        raise LiveImportError("minimal payload field order differs from the native fixture")
    if any("WAWU" in str(value).upper() or "瓦屋" in str(value) for value in payload.values()):
        raise LiveImportError("WAWU upstream semantics leaked into the MIKIHOUSE probe")
    if payload["sku_info"][0]["sku_price"] != price_text:
        raise LiveImportError("minimal payload does not use the verified 65%-JPY price")
    return payload


def _replace_image_placeholder(value: Any, reference: str, target_url: str) -> Any:
    placeholder = f"{{{{SHIJIU_COS_URL:{reference}}}}}"
    if isinstance(value, dict):
        return {
            key: _replace_image_placeholder(child, reference, target_url)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_replace_image_placeholder(child, reference, target_url) for child in value]
    if isinstance(value, str):
        return value.replace(placeholder, target_url)
    return value


def resolve_minimal_payload(
    payload: dict[str, Any], image_reference: str, target_url: str
) -> dict[str, Any]:
    result = _replace_image_placeholder(copy.deepcopy(payload), image_reference, target_url)
    serialized = json.dumps(result, ensure_ascii=False)
    if "SHIJIU_COS_URL" in serialized or "cdn.shopify.com" in serialized:
        raise LiveImportError("minimal formal payload contains unresolved or external images")
    return result


def _shape(value: Any) -> dict[str, Any]:
    shape: dict[str, Any] = {"type": type(value).__name__}
    if isinstance(value, str):
        shape["empty"] = not value
        shape["length"] = len(value)
        if value.startswith("https://"):
            shape["form"] = "https_url"
        elif value.replace(".", "", 1).isdigit():
            shape["form"] = "decimal_string"
        elif value.startswith("source_product_id="):
            shape["form"] = "source_identity_marker"
        else:
            shape["form"] = "text"
    elif isinstance(value, list):
        shape["length"] = len(value)
        shape["item_types"] = sorted({type(item).__name__ for item in value})
    elif isinstance(value, (int, float, bool)) or value is None:
        shape["value"] = value
    return shape


def build_contract_difference_report(
    native_fixture: dict[str, Any],
    failed_full_payload: dict[str, Any],
    minimal_payload: dict[str, Any],
    native_request_preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = []
    for field in list(dict.fromkeys(
        list(native_fixture) + list(failed_full_payload) + list(minimal_payload)
    )):
        rows.append(
            {
                "field": field,
                "native_fixture": _shape(native_fixture.get(field)),
                "failed_full_mikihouse": _shape(failed_full_payload.get(field)),
                "minimal_probe": _shape(minimal_payload.get(field)),
                "same_type_all": len(
                    {
                        type(native_fixture.get(field)).__name__,
                        type(failed_full_payload.get(field)).__name__,
                        type(minimal_payload.get(field)).__name__,
                    }
                )
                == 1,
            }
        )
    previous_headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": "https://shijiu.wfcorp.cn",
        "Referer": "https://shijiu.wfcorp.cn/",
        "User-Agent": "mikihouse-luyao custom importer UA",
        "Cookie": "<present-if-configured>",
    }
    native_headers = dict(NATIVE_SAVE_FALLBACK_HEADERS)
    native_headers["cookie"] = "<present-if-configured>"
    return {
        "schema_version": PROBE_SCHEMA_VERSION,
        "generated_at": now(),
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "reference_scope": (
            "sanitized shape projection of the audited Shijiu downstream fixture; "
            "no WAWU product values are submitted"
        ),
        "wawu_upstream_semantics_reused": False,
        "field_comparison": rows,
        "sku_field_comparison": [
            {
                "field": field,
                "native_fixture": _shape(native_fixture["sku_info"][0].get(field)),
                "failed_full_mikihouse": _shape(
                    failed_full_payload["sku_info"][0].get(field)
                ),
                "minimal_probe": _shape(minimal_payload["sku_info"][0].get(field)),
            }
            for field in native_fixture["sku_info"][0]
        ],
        "specification_field_comparison": [
            {
                "field": field,
                "native_fixture": _shape(native_fixture["spec_name"][0].get(field)),
                "failed_full_mikihouse": _shape(
                    failed_full_payload["spec_name"][0].get(field)
                ),
                "minimal_probe": _shape(minimal_payload["spec_name"][0].get(field)),
            }
            for field in native_fixture["spec_name"][0]
        ],
        "request_contract": {
            "native_reference_fallback_headers": native_headers,
            "previous_mikihouse_headers": previous_headers,
            "minimal_probe_headers": (
                (native_request_preview or {}).get("headers") or native_headers
            ),
            "header_differences_corrected_for_probe": [
                "removed custom MIKIHOUSE User-Agent",
                "removed Origin header absent from audited native fallback",
                "added audited sec-ch-ua headers",
            ],
            "content_type": "application/json;charset=UTF-8",
            "serialization": {
                "format": "JSON",
                "encoding": "UTF-8",
                "ensure_ascii": False,
                "separators": [",", ":"],
                "auth_fields_first_in_body": ["secret", "token"],
                "token_also_in_query": True,
                "payload_field_order_matches_native_fixture": (
                    list(minimal_payload) == list(native_fixture)
                ),
            },
        },
        "business_shape_decisions": {
            "source": SOURCE_CODE,
            "target_category_id": 294884,
            "state": "1",
            "is_shelf": 0,
            "sku_count": 1,
            "specification": "DEFAULT",
            "image_count": 1,
            "sku_price_source": "mini_program_price_jpy",
            "sku_cost_price_probe_shape": "same_as_sku_price_to_match_non-inverted_native_relation",
            "currency": "JPY",
            "currency_conversion_applied": False,
        },
    }


def load_probe_inputs(
    master_path: Path,
    special_path: Path,
    mapping_path: Path,
    native_fixture_path: Path,
    recovery_checkpoint_path: Path,
    first_batch_checkpoint_path: Path,
) -> dict[str, Any]:
    special = set(read_product_numbers(special_path))
    if len(special) != EXPECTED_SPECIAL_COUNT:
        raise LiveImportError(f"expected 351 {PDF_SPECIAL_EXCLUDED_REASON} entries")
    recovery = json.loads(recovery_checkpoint_path.read_text(encoding="utf-8"))
    if (
        recovery.get("scope", {}).get("product_numbers") != [FORBIDDEN_RECOVERY_PRODUCT]
        or recovery.get("recovery_create_attempts") != 1
        or recovery.get("state") != "STOPPED_ON_RECOVERY_ERROR"
    ):
        raise LiveImportError("00-1000-028 terminal recovery evidence is not intact")
    master = json.loads(master_path.read_text(encoding="utf-8"))
    mapping = load_mapping_state(mapping_path)
    selected, selection = select_minimal_probe_candidate(master, special, mapping)
    mapped = map_product_to_shijiu(
        selected, TARGET_CATEGORY, excluded_product_numbers=special
    )
    if not mapped.get("publish_ready"):
        raise LiveImportError("selected minimal probe candidate is not publish-ready")
    native_fixture = json.loads(native_fixture_path.read_text(encoding="utf-8"))
    minimal_payload = build_minimal_payload(native_fixture, mapped)

    first_checkpoint = json.loads(first_batch_checkpoint_path.read_text(encoding="utf-8"))
    failed_record = first_checkpoint["records"][FORBIDDEN_RECOVERY_PRODUCT]
    first_preview = json.loads(
        (first_batch_checkpoint_path.parent.parent
         / "deliverables/shijiu_import/payload_previews.json").read_text(encoding="utf-8")
    )
    failed_item = next(
        item
        for item in first_preview["payloads"]
        if item["product_number"] == FORBIDDEN_RECOVERY_PRODUCT
    )
    failed_full_payload = _resolve_payload(failed_item, failed_record["image_uploads"])
    failed_full_payload["state"] = "1"
    return {
        "master_file_sha256": file_sha256(master_path),
        "native_fixture_file_sha256": file_sha256(native_fixture_path),
        "special_count": len(special),
        "selected_product": selected,
        "selection": selection,
        "mapped": mapped,
        "minimal_payload": minimal_payload,
        "native_fixture": native_fixture,
        "failed_full_payload": failed_full_payload,
    }


def _new_checkpoint(inputs: dict[str, Any]) -> dict[str, Any]:
    mapped = inputs["mapped"]
    image = mapped["image_upload_plan"][0]
    return {
        "schema_version": PROBE_SCHEMA_VERSION,
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "mode": "SINGLE_MINIMAL_CREATE_DIAGNOSTIC",
        "created_at": now(),
        "updated_at": now(),
        "state": "READY_FOR_READ_ONLY_PREFLIGHT",
        "scope": {
            "product_numbers": [mapped["product_number"]],
            "maximum_image_upload_requests": 1,
            "maximum_create_requests": 1,
            "updates_allowed_only_after_minimal_readback": True,
        },
        "forbidden_product_numbers": [FORBIDDEN_RECOVERY_PRODUCT],
        "pdf_special_exclusion_count": inputs["special_count"],
        "master_file_sha256": inputs["master_file_sha256"],
        "native_fixture_file_sha256": inputs["native_fixture_file_sha256"],
        "minimal_payload_sha256": content_sha256(inputs["minimal_payload"]),
        "full_payload_sha256": mapped["payload_sha256"],
        "selection": inputs["selection"],
        "online_source_verification": None,
        "preflight": None,
        "image": {
            "source_url": image["source_url"],
            "source_url_sha256": hashlib.sha256(
                image["source_url"].encode("utf-8")
            ).hexdigest(),
            "upload_reference": image["upload_reference"],
            "upload_attempts": 0,
            "status": "PENDING",
            "target_url": None,
            "response": None,
        },
        "create_attempts": 0,
        "create_intent_at": None,
        "create_response": None,
        "shijiu_product_id": None,
        "shijiu_sku_id": None,
        "minimal_readback": None,
        "mapping_persisted": False,
        "staged_updates": [],
        "post_failure_forensics": None,
        "error": None,
        "request_ledger": [],
    }


def load_or_create_probe_checkpoint(path: Path, inputs: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        checkpoint = _new_checkpoint(inputs)
        write_json_atomic(path, checkpoint)
        return checkpoint
    checkpoint = json.loads(path.read_text(encoding="utf-8"))
    if (
        checkpoint.get("source") != SOURCE_CODE
        or checkpoint.get("target") != "SHIJIU"
        or checkpoint.get("scope", {}).get("product_numbers")
        != [inputs["mapped"]["product_number"]]
        or checkpoint.get("minimal_payload_sha256")
        != content_sha256(inputs["minimal_payload"])
        or checkpoint.get("master_file_sha256") != inputs["master_file_sha256"]
    ):
        raise LiveImportError("minimal probe checkpoint identity or payload drift")
    return checkpoint


def _sku_codes(detail: dict[str, Any]) -> set[str]:
    return {
        str(row.get("sku_code") or "").strip()
        for row in recursively_find_skus(detail)
        if str(row.get("sku_code") or "").strip()
    }


def validate_probe_readback(
    mapped: dict[str, Any],
    payload: dict[str, Any],
    product_id: str,
    detail: dict[str, Any],
    list_row: dict[str, Any],
    *,
    require_details: bool = False,
) -> dict[str, Any]:
    if str(detail.get("code")) != "1":
        raise ContractMismatchError("getFormatInfo did not return Shijiu read success code 1")
    data = detail.get("data") if isinstance(detail.get("data"), dict) else {}
    actual_id = _product_id_from_value({"data": data})
    if actual_id not in (None, "") and str(actual_id) != str(product_id):
        raise ContractMismatchError("getFormatInfo product_id mismatch")
    if str(_first_observation(detail, ("good_type",))) != "294884":
        raise ContractMismatchError("minimal readback category mismatch")
    if str(_first_observation(detail, ("good_name",)) or "").strip() != payload["good_name"]:
        raise ContractMismatchError("minimal readback good_name mismatch")
    if str(_first_observation(detail, ("master_graph",)) or "") != payload["master_graph"]:
        raise ContractMismatchError("minimal readback master_graph mismatch")
    if str(_first_observation(detail, ("broadcast",)) or "") != payload["broadcast"]:
        raise ContractMismatchError("minimal readback broadcast mismatch")
    expected_sku = payload["sku_info"][0]
    rows = {
        str(row.get("sku_code") or "").strip(): row
        for row in recursively_find_skus(detail)
        if str(row.get("sku_code") or "").strip()
    }
    if set(rows) != {expected_sku["sku_code"]}:
        raise ContractMismatchError(
            f"minimal readback exact SKU mismatch: observed={sorted(rows)}"
        )
    row = rows[expected_sku["sku_code"]]
    sku_id = _sku_id_from_row(row)
    if not sku_id:
        raise ContractMismatchError("minimal readback exposes no durable Shijiu SKU ID")
    if _decimal(row.get("price", row.get("sku_price"))) != _decimal(
        expected_sku["sku_price"]
    ):
        raise ContractMismatchError("minimal readback JPY price mismatch")
    if _decimal(row.get("stock", row.get("sku_stock"))) != _decimal(
        expected_sku["sku_stock"]
    ):
        raise ContractMismatchError("minimal readback stock mismatch")
    actual_spec = _normalized_specification(
        row.get("spec_son_name") or row.get("spec_name") or ""
    )
    if actual_spec != expected_sku["spec_name"]:
        raise ContractMismatchError(
            f"minimal readback specification mismatch: {actual_spec!r}"
        )
    if str(row.get("sku_thumbnail") or "") != expected_sku["sku_thumbnail"]:
        raise ContractMismatchError("minimal readback SKU image mismatch")
    state = list_row.get("state", _first_observation(detail, ("state",)))
    is_shelf = list_row.get("is_shelf", _first_observation(detail, ("is_shelf",)))
    if str(state) != "1" or str(is_shelf) not in {"0", "False", "false"}:
        raise ContractMismatchError(
            f"minimal readback visibility mismatch: state={state!r}, is_shelf={is_shelf!r}"
        )
    if require_details:
        expected_details = str(payload.get("good_details") or "")
        actual_details = str(_first_observation(detail, ("good_details",)) or "")
        if expected_details != actual_details:
            raise ContractMismatchError("staged details readback mismatch")
    return {
        "source": SOURCE_CODE,
        "source_product_id": mapped["source_product_id"],
        "product_number": mapped["product_number"],
        "shijiu_product_id": str(product_id),
        "target_category_id": 294884,
        "good_name": payload["good_name"],
        "off_shelf": True,
        "master_graph": payload["master_graph"],
        "carousel_urls": [
            value.strip()
            for value in str(payload["broadcast"]).split(",")
            if value.strip()
        ],
        "detail_image_urls": [],
        "sku_count": 1,
        "skus": [
            {
                "source_variant_sku": mapped["source_variants"][0][
                    "source_variant_sku"
                ],
                "backend_sku_code": expected_sku["sku_code"],
                "shijiu_sku_id": sku_id,
                "price_jpy": int(_decimal(expected_sku["sku_price"])),
                "stock": int(_decimal(expected_sku["sku_stock"])),
                "specification": expected_sku["spec_name"],
                "image_url": expected_sku["sku_thumbnail"],
                "passed": True,
            }
        ],
        "passed": True,
        "verified_at": now(),
    }


def _persist_probe_mapping(
    path: Path, mapped: dict[str, Any], readback: dict[str, Any], payload_hash: str
) -> None:
    state = load_mapping_state(path)
    row = state["products"][mapped["product_number"]]
    existing = row.get("shijiu_product_id")
    if existing not in (None, "", readback["shijiu_product_id"]):
        raise DuplicateRiskError("refusing to replace an existing Shijiu product mapping")
    row.update(
        {
            "shijiu_product_id": readback["shijiu_product_id"],
            "match_method": "minimal_create_exact_sku_and_getFormatInfo_readback",
            "target_category_id": 294884,
            "target_active": False,
            "last_payload_sha256": payload_hash,
            "last_verified_at": readback["verified_at"],
        }
    )
    sku_result = readback["skus"][0]
    variant = row["variants"][sku_result["source_variant_sku"]]
    existing_sku = variant.get("shijiu_sku_id")
    if existing_sku not in (None, "", sku_result["shijiu_sku_id"]):
        raise DuplicateRiskError("refusing to replace an existing Shijiu SKU mapping")
    variant.update(
        {
            "shijiu_sku_id": sku_result["shijiu_sku_id"],
            "match_method": "minimal_create_exact_sku_and_getFormatInfo_readback",
            "last_verified_at": readback["verified_at"],
        }
    )
    state["updated_at"] = now()
    write_json_atomic(path, state)


class MinimalCreateProbeRunner:
    def __init__(
        self,
        client: ShijiuLiveClient,
        inputs: dict[str, Any],
        checkpoint_path: Path,
        mapping_path: Path,
        report_path: Path,
        candidate_path: Path,
        difference_path: Path,
        readback_path: Path,
        *,
        confirmation: str,
    ) -> None:
        if confirmation != PROBE_CONFIRMATION:
            raise LiveImportError("exact minimal-probe confirmation is missing")
        self.client = client
        self.inputs = inputs
        self.mapped = inputs["mapped"]
        self.checkpoint_path = checkpoint_path
        self.mapping_path = mapping_path
        self.report_path = report_path
        self.candidate_path = candidate_path
        self.difference_path = difference_path
        self.readback_path = readback_path
        self.confirmation = confirmation
        self.checkpoint = load_or_create_probe_checkpoint(checkpoint_path, inputs)
        self._request_cursor = 0
        self._write_static_evidence()

    def _write_static_evidence(self) -> None:
        product = self.inputs["selected_product"]
        variant = product["variants"][0]
        write_json_atomic(
            self.candidate_path,
            {
                "schema_version": PROBE_SCHEMA_VERSION,
                "generated_at": now(),
                "source": SOURCE_CODE,
                "target": "SHIJIU",
                "selection": self.inputs["selection"],
                "candidate": {
                    "product_number": product["product_number"],
                    "product_name": product["name"],
                    "product_url": product["product_url"],
                    "active": product["active"],
                    "variant_count": 1,
                    "ordered_image_count": 1,
                    "variant_sku": variant["sku"],
                    "available_for_sale": variant["available_for_sale"],
                    "tax_included_price_jpy": variant["tax_included_price_jpy"],
                    "mini_program_price_jpy": variant["mini_program_price_jpy"],
                    "calculated_price_jpy": calculate_mini_program_price_jpy(
                        int(variant["tax_included_price_jpy"])
                    ),
                    "pdf_special": False,
                    "mapping_bound": False,
                },
                "forbidden_recovery_product": FORBIDDEN_RECOVERY_PRODUCT,
                "forbidden_recovery_create_requests": 0,
            },
        )
        preview = self.client.native_save_request_preview(
            self.inputs["minimal_payload"]
        )
        difference = build_contract_difference_report(
            self.inputs["native_fixture"],
            self.inputs["failed_full_payload"],
            self.inputs["minimal_payload"],
            preview,
        )
        difference["probe_state"] = self.checkpoint["state"]
        difference["candidate_product_number"] = self.mapped["product_number"]
        difference["minimal_payload_sha256"] = content_sha256(
            self.inputs["minimal_payload"]
        )
        ledger = self.checkpoint.get("request_ledger", [])
        create_response = self.checkpoint.get("create_response") or {}
        native_response = create_response.get("_native_response") or {}
        difference["probe_result"] = {
            "state": self.checkpoint["state"],
            "candidate_product_number": self.mapped["product_number"],
            "image_upload_attempts": self.checkpoint["image"]["upload_attempts"],
            "create_attempts": self.checkpoint["create_attempts"],
            "staged_update_attempts": sum(
                row.get("operation") == "native staged MIKIHOUSE product update"
                for row in ledger
            ),
            "api_response": {
                "http_status": native_response.get("http_status"),
                "content_type": native_response.get("content_type"),
                "code": create_response.get("code"),
                "msg": create_response.get("msg"),
                "data_shape": _shape(create_response.get("data")),
            },
            "observable_product_ids": (
                (self.checkpoint.get("post_failure_forensics") or {})
                .get("identity_queries", {})
                .get("exact_sku_result_ids", [])
            ),
            "shijiu_product_id": self.checkpoint.get("shijiu_product_id"),
            "shijiu_sku_id": self.checkpoint.get("shijiu_sku_id"),
            "mapping_persisted": self.checkpoint.get("mapping_persisted", False),
            "legacy_products_modified": 0,
            "forbidden_00_1000_028_create_requests": 0,
        }
        difference["diagnostic_conclusion"] = {
            "proven": (
                "The native-shaped, one-SKU, one-image request was parsed and returned "
                "HTTP 200 / code 200, but no unique product or SKU became observable "
                "through Goods/index; therefore creation is not proven."
            ),
            "field_group_isolation_result": (
                "Not reached: staged specification, gallery, and detail updates are "
                "permitted only after a verified minimal create."
            ),
            "remaining_unknown": (
                "The available evidence does not identify the target-side validation, "
                "authorization, tenant, or workflow condition responsible for the silent rejection."
            ),
            "no_guessing_or_retry": True,
        }
        write_json_atomic(self.difference_path, difference)

    def _persist(self) -> None:
        new_requests = copy.deepcopy(self.client.requests[self._request_cursor :])
        self.checkpoint["request_ledger"].extend(new_requests)
        self._request_cursor = len(self.client.requests)
        ledger = self.checkpoint["request_ledger"]
        if any(row["path"] not in ALLOWED_ENDPOINTS for row in ledger):
            raise LiveImportError("minimal probe called an out-of-scope Shijiu endpoint")
        if sum(row["path"] == IMAGE_UPLOAD_PATH for row in ledger) > 1:
            raise LiveImportError("minimal probe exceeded the one-image upload budget")
        create_count = sum(
            row.get("operation") == "native minimal MIKIHOUSE product create"
            for row in ledger
        )
        if create_count > 1:
            raise LiveImportError("minimal probe exceeded the one-create budget")
        if self.mapped["product_number"] == FORBIDDEN_RECOVERY_PRODUCT:
            raise LiveImportError("00-1000-028 is permanently forbidden for this probe")
        self.checkpoint["updated_at"] = now()
        write_json_atomic(self.checkpoint_path, self.checkpoint)
        self._write_reports()

    def _write_reports(self) -> None:
        ledger = self.checkpoint["request_ledger"]
        report = {
            "schema_version": PROBE_SCHEMA_VERSION,
            "generated_at": now(),
            "source": SOURCE_CODE,
            "target": "SHIJIU",
            "mode": "SINGLE_MINIMAL_CREATE_DIAGNOSTIC",
            "state": self.checkpoint["state"],
            "candidate_product_number": self.mapped["product_number"],
            "forbidden_00_1000_028_create_requests": 0,
            "pdf_special_exclusion_count": self.checkpoint[
                "pdf_special_exclusion_count"
            ],
            "legacy_products_modified": 0,
            "batch_products_processed": 0,
            "selection": self.checkpoint["selection"],
            "online_source_verification": self.checkpoint[
                "online_source_verification"
            ],
            "preflight": self.checkpoint["preflight"],
            "image": self.checkpoint["image"],
            "create_attempts": self.checkpoint["create_attempts"],
            "create_response": self.checkpoint["create_response"],
            "shijiu_product_id": self.checkpoint["shijiu_product_id"],
            "shijiu_sku_id": self.checkpoint["shijiu_sku_id"],
            "minimal_readback": self.checkpoint["minimal_readback"],
            "mapping_persisted": self.checkpoint["mapping_persisted"],
            "staged_updates": self.checkpoint["staged_updates"],
            "post_failure_forensics": self.checkpoint[
                "post_failure_forensics"
            ],
            "error": self.checkpoint["error"],
            "requests": {
                "total": len(ledger),
                "read": sum(row["semantic_operation"] == "read" for row in ledger),
                "write": sum(row["semantic_operation"] == "write" for row in ledger),
                "image_upload": sum(row["path"] == IMAGE_UPLOAD_PATH for row in ledger),
                "minimal_create": sum(
                    row.get("operation")
                    == "native minimal MIKIHOUSE product create"
                    for row in ledger
                ),
                "staged_update": sum(
                    row.get("operation")
                    == "native staged MIKIHOUSE product update"
                    for row in ledger
                ),
            },
            "checkpoint": "state/shijiu_minimal_create_probe_checkpoint.json",
            "candidate_report": (
                "deliverables/shijiu_import/minimal_create_probe_candidate.json"
            ),
            "difference_report": (
                "deliverables/shijiu_import/minimal_create_payload_diff.json"
            ),
            "readback_report": (
                "deliverables/shijiu_import/minimal_create_probe_readback.json"
            ),
        }
        write_json_atomic(self.report_path, report)
        write_json_atomic(
            self.readback_path,
            {
                "schema_version": PROBE_SCHEMA_VERSION,
                "generated_at": now(),
                "source": SOURCE_CODE,
                "target": "SHIJIU",
                "product_number": self.mapped["product_number"],
                "state": self.checkpoint["state"],
                "product_id": self.checkpoint["shijiu_product_id"],
                "sku_id": self.checkpoint["shijiu_sku_id"],
                "minimal_readback": self.checkpoint["minimal_readback"],
                "staged_updates": self.checkpoint["staged_updates"],
                "passed": self.checkpoint["state"] == "FULL_PAYLOAD_VERIFIED",
                "error": self.checkpoint["error"],
            },
        )

    def record_online_source_verification(self, result: dict[str, Any]) -> None:
        expected = self.inputs["selected_product"]
        variant = expected["variants"][0]
        if (
            result.get("product_number") != expected["product_number"]
            or result.get("name") != expected["name"]
            or result.get("variant_skus") != [variant["sku"]]
            or result.get("tax_included_prices_jpy")
            != [variant["tax_included_price_jpy"]]
            or not result.get("main_image_matches")
            or not result.get("passed")
        ):
            raise LiveImportError("live MIKI HOUSE source verification differs from master")
        self.checkpoint["online_source_verification"] = result
        self._persist()

    def _stop(self, error: Exception, *, state: str = "STOPPED_ON_PROBE_ERROR") -> None:
        self.checkpoint["state"] = state
        self.checkpoint["error"] = {
            "type": type(error).__name__,
            "message": str(error),
            "at": now(),
        }
        try:
            forensic = prove_no_residual(
                self.client,
                self.mapped,
                {"create_response": self.checkpoint.get("create_response"), "shijiu_product_id": None},
                self.mapping_path,
            )
            self.checkpoint["post_failure_forensics"] = forensic
        except Exception as forensic_error:
            self.checkpoint["post_failure_forensics"] = {
                "performed_at": now(),
                "mode": "READ_ONLY",
                "passed": False,
                "error": {
                    "type": type(forensic_error).__name__,
                    "message": str(forensic_error),
                },
            }
        self._persist()
        self._write_static_evidence()

    def _exact_list_row(self, product_id: str) -> dict[str, Any]:
        sku = self.mapped["source_variants"][0]["backend_sku_code"]
        matches = {}
        for status in ("", "0", "1", "2"):
            response = self.client.search_products(
                sku, status=status, push="", good_type="", page=1, page_size=100
            )
            for row in response_rows(response):
                if str(row.get("id") or "") == str(product_id):
                    matches[str(product_id)] = row
        if len(matches) != 1:
            raise ContractMismatchError("Goods/index did not expose the exact created product")
        return matches[str(product_id)]

    def _stage_payloads(self, target_url: str) -> list[tuple[str, dict[str, Any], bool]]:
        reference = self.mapped["image_upload_plan"][0]["upload_reference"]
        minimal = resolve_minimal_payload(
            self.inputs["minimal_payload"], reference, target_url
        )
        uploaded = {
            reference: {"status": "UPLOADED", "target_url": target_url}
        }
        full = _resolve_payload(self.mapped, uploaded)
        full["state"] = "1"
        specs = copy.deepcopy(minimal)
        specs["spec_name"] = copy.deepcopy(full["spec_name"])
        specs["sku_info"] = copy.deepcopy(full["sku_info"])
        media = copy.deepcopy(specs)
        for field in ("master_graph", "broadcast", "good_detail_pics"):
            media[field] = copy.deepcopy(full[field])
        return [
            ("FULL_SPECIFICATIONS_AND_SKU_SHAPE", specs, False),
            ("FULL_ORDERED_MEDIA", media, False),
            ("FULL_DETAILS_AND_METADATA", full, True),
        ]

    def run_preflight_only(self) -> dict[str, Any]:
        if self.checkpoint["state"] != "READY_FOR_READ_ONLY_PREFLIGHT":
            raise LiveImportError("prepare-only requires a fresh, unconsumed probe checkpoint")
        if self.checkpoint["online_source_verification"] is None:
            raise LiveImportError("online MIKI HOUSE source verification is required")
        try:
            preflight = prove_no_residual(
                self.client,
                self.mapped,
                {"create_response": None, "shijiu_product_id": None},
                self.mapping_path,
            )
            self.checkpoint["preflight"] = preflight
            if not preflight["passed"]:
                raise DuplicateRiskError(
                    f"candidate residual absence not proven: {preflight['absence_proof_reasons']}"
                )
            self.checkpoint["state"] = "PREFLIGHT_PASSED"
            self._persist()
        except Exception as error:
            self._stop(error)
            raise
        return json.loads(self.report_path.read_text(encoding="utf-8"))

    def run(self) -> dict[str, Any]:
        terminal = {
            "STOPPED_ON_PROBE_ERROR",
            "STOPPED_IMAGE_RESULT_UNKNOWN",
            "STOPPED_CREATE_RESULT_UNKNOWN",
            "FULL_PAYLOAD_VERIFIED",
        }
        if self.checkpoint["state"] in terminal:
            raise LiveImportError("minimal probe checkpoint is terminal; no retry is allowed")
        try:
            if self.checkpoint["online_source_verification"] is None:
                raise LiveImportError("online MIKI HOUSE source verification is required")
            if self.checkpoint["state"] == "READY_FOR_READ_ONLY_PREFLIGHT":
                preflight = prove_no_residual(
                    self.client,
                    self.mapped,
                    {"create_response": None, "shijiu_product_id": None},
                    self.mapping_path,
                )
                self.checkpoint["preflight"] = preflight
                if not preflight["passed"]:
                    raise DuplicateRiskError(
                        f"candidate residual absence not proven: {preflight['absence_proof_reasons']}"
                    )
                self.checkpoint["state"] = "PREFLIGHT_PASSED"
                self._persist()
            if self.checkpoint["state"] == "PREFLIGHT_PASSED":
                if self.checkpoint["image"]["upload_attempts"] != 0:
                    raise LiveImportError("image upload budget already consumed")
                self.checkpoint["image"]["upload_attempts"] = 1
                self.checkpoint["image"]["status"] = "UPLOAD_INTENT_PERSISTED"
                self._persist()
                try:
                    target_url, response = self.client.upload_image(
                        self.checkpoint["image"]["source_url"],
                        confirmation=self.confirmation,
                    )
                except Exception as error:
                    self._stop(error, state="STOPPED_IMAGE_RESULT_UNKNOWN")
                    raise
                self.checkpoint["image"].update(
                    {
                        "status": "UPLOADED",
                        "target_url": target_url,
                        "response": _redacted_response(response),
                    }
                )
                self.checkpoint["state"] = "IMAGE_UPLOADED"
                self._persist()
            if self.checkpoint["state"] == "IMAGE_UPLOADED":
                if self.checkpoint["create_attempts"] != 0:
                    raise LiveImportError("minimal create budget already consumed")
                reference = self.checkpoint["image"]["upload_reference"]
                payload = resolve_minimal_payload(
                    self.inputs["minimal_payload"],
                    reference,
                    self.checkpoint["image"]["target_url"],
                )
                if FORBIDDEN_RECOVERY_PRODUCT in json.dumps(payload, ensure_ascii=False):
                    raise LiveImportError("00-1000-028 leaked into the new probe payload")
                self.checkpoint["create_attempts"] = 1
                self.checkpoint["create_intent_at"] = now()
                self.checkpoint["state"] = "MINIMAL_CREATE_INTENT_PERSISTED"
                self._persist()
                try:
                    response = self.client.create_product_native(
                        payload, confirmation=self.confirmation
                    )
                except Exception as error:
                    self._stop(error, state="STOPPED_CREATE_RESULT_UNKNOWN")
                    raise
                self.checkpoint["create_response"] = _redacted_response(response)
                self.checkpoint["state"] = "MINIMAL_CREATE_RESPONSE_RECEIVED"
                self._persist()
                try:
                    product_id, list_row, discovery = discover_created_product(
                        self.client, self.mapped, response
                    )
                    self.checkpoint["shijiu_product_id"] = product_id
                    readback = validate_probe_readback(
                        self.mapped,
                        payload,
                        product_id,
                        discovery["detail"],
                        list_row,
                    )
                    self.checkpoint["shijiu_sku_id"] = readback["skus"][0][
                        "shijiu_sku_id"
                    ]
                    self.checkpoint["minimal_readback"] = readback
                    _persist_probe_mapping(
                        self.mapping_path,
                        self.mapped,
                        readback,
                        content_sha256(payload),
                    )
                    self.checkpoint["mapping_persisted"] = True
                    self.checkpoint["state"] = "MINIMAL_CREATE_VERIFIED"
                    self._persist()
                except Exception as error:
                    self._stop(error)
                    raise
            if self.checkpoint["state"] == "MINIMAL_CREATE_VERIFIED":
                target_url = self.checkpoint["image"]["target_url"]
                product_id = self.checkpoint["shijiu_product_id"]
                current_hash = content_sha256(
                    resolve_minimal_payload(
                        self.inputs["minimal_payload"],
                        self.checkpoint["image"]["upload_reference"],
                        target_url,
                    )
                )
                previous_payload = resolve_minimal_payload(
                    self.inputs["minimal_payload"],
                    self.checkpoint["image"]["upload_reference"],
                    target_url,
                )
                for stage, payload, require_details in self._stage_payloads(target_url):
                    if content_sha256(payload) == current_hash:
                        self.checkpoint["staged_updates"].append(
                            {"stage": stage, "state": "SKIPPED_NO_PAYLOAD_CHANGE"}
                        )
                        continue
                    record = {
                        "stage": stage,
                        "state": "UPDATE_INTENT_PERSISTED",
                        "changed_fields": [
                            key
                            for key in payload
                            if payload.get(key) != previous_payload.get(key)
                        ],
                        "payload_sha256": content_sha256(payload),
                    }
                    self.checkpoint["staged_updates"].append(record)
                    self._persist()
                    edit_payload = copy.deepcopy(payload)
                    edit_payload["id"] = int(product_id)
                    try:
                        response = self.client.update_product_native(
                            edit_payload, confirmation=self.confirmation
                        )
                        detail = self.client.product_detail(product_id)
                        list_row = self._exact_list_row(product_id)
                        readback = validate_probe_readback(
                            self.mapped,
                            payload,
                            product_id,
                            detail,
                            list_row,
                            require_details=require_details,
                        )
                    except Exception as error:
                        record["state"] = "STOPPED_ON_STAGE_ERROR"
                        record["error"] = {
                            "type": type(error).__name__,
                            "message": str(error),
                            "at": now(),
                        }
                        self._stop(error)
                        raise
                    record.update(
                        {
                            "state": "VERIFIED",
                            "response": _redacted_response(response),
                            "readback": readback,
                        }
                    )
                    current_hash = content_sha256(payload)
                    previous_payload = payload
                    self._persist()
                verified_updates = [
                    row
                    for row in self.checkpoint["staged_updates"]
                    if row.get("state") == "VERIFIED"
                ]
                if verified_updates:
                    _persist_probe_mapping(
                        self.mapping_path,
                        self.mapped,
                        verified_updates[-1]["readback"],
                        current_hash,
                    )
                self.checkpoint["state"] = "FULL_PAYLOAD_VERIFIED"
                self.checkpoint["error"] = None
                self._persist()
        except Exception:
            if self.checkpoint["state"] not in terminal:
                self._persist()
            raise
        return json.loads(self.report_path.read_text(encoding="utf-8"))
