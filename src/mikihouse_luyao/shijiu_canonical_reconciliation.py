from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from .csv_input import read_product_numbers
from .shijiu_import import (
    EXPECTED_SPECIAL_COUNT,
    PDF_SPECIAL_EXCLUDED_REASON,
    SOURCE_CODE,
    content_sha256,
    load_mapping_state,
    map_product_to_shijiu,
    now,
    write_json_atomic,
)
from .shijiu_live_import import (
    ContractMismatchError,
    DuplicateRiskError,
    LiveImportError,
    ShijiuLiveClient,
    _resolve_payload,
    _row_product_id,
    _unique_exact_name_product_matches,
    _unique_exact_product_matches,
    persist_verified_mapping,
    verify_exact_name_create_candidates,
)
from .shijiu_minimal_probe import TARGET_CATEGORY
from .shijiu_recovery import complete_category_scan


RECONCILIATION_PRODUCT_NUMBER = "36-2001-572"
EXPECTED_BACKEND_SKU_CODE = "MIKI-36-2001-57200039999"
EXPECTED_PRICE_JPY = 1430
RECONCILIATION_SCHEMA_VERSION = 1


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_reconciliation_inputs(
    master_path: Path,
    special_path: Path,
    mapping_path: Path,
    checkpoint_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], set[str]]:
    special = set(read_product_numbers(special_path))
    if len(special) != EXPECTED_SPECIAL_COUNT:
        raise LiveImportError(f"expected {EXPECTED_SPECIAL_COUNT} {PDF_SPECIAL_EXCLUDED_REASON} rows")
    if RECONCILIATION_PRODUCT_NUMBER in special:
        raise LiveImportError(f"{PDF_SPECIAL_EXCLUDED_REASON}: reconciliation product rejected")
    master = json.loads(master_path.read_text(encoding="utf-8"))
    products = [
        row for row in master.get("products") or []
        if row.get("product_number") == RECONCILIATION_PRODUCT_NUMBER
    ]
    if len(products) != 1:
        raise LiveImportError("master catalog does not contain exactly one reconciliation product")
    item = map_product_to_shijiu(
        products[0], TARGET_CATEGORY, excluded_product_numbers=special
    )
    if len(item.get("source_variants") or []) != 1:
        raise LiveImportError("reconciliation product is no longer single-variant")
    variant = item["source_variants"][0]
    if (
        variant.get("backend_sku_code") != EXPECTED_BACKEND_SKU_CODE
        or variant.get("mini_program_price_jpy") != EXPECTED_PRICE_JPY
    ):
        raise LiveImportError("reconciliation SKU or 65%-JPY price drift")
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    if (
        checkpoint.get("source") != SOURCE_CODE
        or checkpoint.get("target") != "SHIJIU"
        or checkpoint.get("scope", {}).get("product_numbers") != [RECONCILIATION_PRODUCT_NUMBER]
        or checkpoint.get("create_attempts") != 1
        or checkpoint.get("create_response") != {"code": 200, "data": [], "msg": "success"}
        or len(checkpoint.get("image_uploads") or {}) != 1
    ):
        raise LiveImportError("canonical checkpoint does not prove exactly one historical CREATE")
    payload = _resolve_payload(item, checkpoint["image_uploads"])
    if content_sha256(payload) != checkpoint.get("resolved_payload_sha256"):
        raise DuplicateRiskError("reconciliation payload hash differs from the sent CREATE")
    mapping = load_mapping_state(mapping_path)
    mapping_row = mapping.get("products", {}).get(RECONCILIATION_PRODUCT_NUMBER) or {}
    existing_id = mapping_row.get("shijiu_product_id")
    if existing_id not in (None, "", checkpoint.get("shijiu_product_id")):
        raise DuplicateRiskError("mapping and checkpoint product identity disagree")
    return item, payload, checkpoint, special


def _merge_candidate_rows(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for group in groups:
        for row in group:
            product_id = _row_product_id(row)
            if product_id:
                result[product_id] = row
    return list(result.values())


def reconcile_historical_create_read_only(
    client: ShijiuLiveClient,
    item: dict[str, Any],
    payload: dict[str, Any],
    checkpoint: dict[str, Any],
    special: set[str],
    *,
    checkpoint_path: Path,
    mapping_path: Path,
    validation_report_path: Path,
    candidate_report_path: Path,
    reconciliation_report_path: Path,
) -> dict[str, Any]:
    """Reconcile the one historical CREATE without any target mutation."""
    if item.get("product_number") != RECONCILIATION_PRODUCT_NUMBER:
        raise LiveImportError("reconciliation is hard-limited to 36-2001-572")
    if len(special) != EXPECTED_SPECIAL_COUNT or item["product_number"] in special:
        raise LiveImportError(f"{PDF_SPECIAL_EXCLUDED_REASON}: target access blocked")
    if checkpoint.get("create_attempts") != 1:
        raise DuplicateRiskError("reconciliation requires exactly one historical CREATE")
    mapping = load_mapping_state(mapping_path)
    mapping_row = mapping["products"][RECONCILIATION_PRODUCT_NUMBER]
    if mapping_row.get("shijiu_product_id") not in (None, ""):
        if (
            checkpoint.get("status") == "RECONCILED_READBACK_VERIFIED"
            and checkpoint.get("shijiu_product_id") == mapping_row["shijiu_product_id"]
        ):
            return json.loads(reconciliation_report_path.read_text(encoding="utf-8"))
        raise DuplicateRiskError("reconciliation product is already bound without matching evidence")

    before_checkpoint_hash = _file_sha256(checkpoint_path)
    before_mapping_hash = _file_sha256(mapping_path)
    initial_request_count = len(client.requests)
    name_rows, name_evidence = _unique_exact_name_product_matches(
        client, payload["good_name"]
    )
    verified, validations = verify_exact_name_create_candidates(
        client,
        item,
        payload,
        name_rows,
        create_response=checkpoint.get("create_response"),
    )

    scan_summaries: list[dict[str, Any]] = []
    scan_rows: list[dict[str, Any]] = []
    if len(verified) != 1:
        # A full, stable category scan is the fallback when the server-side name
        # filter is missing or ambiguous. It remains strictly read-only.
        for filters in (
            {"label": "push_any", "push": "", "status": ""},
            {"label": "status_0", "push": "2", "status": "0"},
            {"label": "status_1", "push": "2", "status": "1"},
            {"label": "status_2", "push": "2", "status": "2"},
        ):
            summary, rows = complete_category_scan(
                client,
                exact_name=payload["good_name"],
                filters=filters,
            )
            scan_summaries.append(summary)
            scan_rows = _merge_candidate_rows(scan_rows, rows)
        all_rows = _merge_candidate_rows(name_rows, scan_rows)
        verified, validations = verify_exact_name_create_candidates(
            client,
            item,
            payload,
            all_rows,
            create_response=checkpoint.get("create_response"),
        )
    else:
        all_rows = name_rows

    auxiliary_rows = _unique_exact_product_matches(client, EXPECTED_BACKEND_SKU_CODE)
    auxiliary_ids = sorted({_row_product_id(row) for row in auxiliary_rows if _row_product_id(row)})
    current_requests = copy.deepcopy(client.requests[initial_request_count:])
    if any(row.get("semantic_operation") != "read" for row in current_requests):
        raise LiveImportError("reconciliation attempted a non-read Shijiu request")
    if len(verified) > 1:
        status = "RECONCILIATION_AMBIGUOUS_STRONG_EVIDENCE"
    elif not verified:
        status = "RECONCILIATION_NO_UNIQUE_STRONG_EVIDENCE"
    else:
        status = "RECONCILED_READBACK_VERIFIED"

    readback = verified[0]["readback"] if len(verified) == 1 else None
    if readback:
        # The official readback contract proves backend sku_code but does not
        # expose a documented SKU id. Never infer one from a generic nested id.
        for sku in readback["skus"]:
            sku["shijiu_sku_id"] = None
        persist_verified_mapping(
            mapping_path,
            item,
            readback,
            content_sha256(payload),
        )

    report = {
        "schema_version": RECONCILIATION_SCHEMA_VERSION,
        "generated_at": now(),
        "mode": "HISTORICAL_CREATE_STRICT_READ_ONLY_RECONCILIATION",
        "status": status,
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "product_number": RECONCILIATION_PRODUCT_NUMBER,
        "exact_good_name": payload["good_name"],
        "target_category_id": 294884,
        "expected_backend_sku_code": EXPECTED_BACKEND_SKU_CODE,
        "expected_price_jpy": EXPECTED_PRICE_JPY,
        "currency": "JPY",
        "historical_create_attempts": checkpoint["create_attempts"],
        "create_requests_this_run": 0,
        "image_upload_requests_this_run": 0,
        "update_requests_this_run": 0,
        "target_mutations_this_run": 0,
        "read_requests_this_run": len(current_requests),
        "primary_discovery": name_evidence,
        "full_category_scan_used": bool(scan_summaries),
        "full_category_scans": scan_summaries,
        "candidate_product_ids": sorted({_row_product_id(row) for row in all_rows}),
        "candidate_validations": validations,
        "verified_product_ids": [
            row["readback"]["shijiu_product_id"] for row in verified
        ],
        "auxiliary_good_code_product_ids": auxiliary_ids,
        "good_code_role": "auxiliary_only_never_binding",
        "mapping_persisted": bool(readback),
        "shijiu_product_id": readback["shijiu_product_id"] if readback else None,
        "shijiu_sku_id": None,
        "backend_sku_code_verified": bool(readback),
        "price_verified": bool(readback),
        "specification_verified": bool(readback),
        "images_verified": bool(readback),
        "pdf_special_exclusion_count": len(special),
        "legacy_product_mutations": 0,
        "additional_mikihouse_products_touched": 0,
        "protected_state_previous_sha256": {
            "checkpoint": before_checkpoint_hash,
            "mapping": before_mapping_hash,
        },
        "sensitive_values_included": False,
    }

    prior_error = copy.deepcopy(checkpoint.get("error"))
    current_error = None if readback else {
        "type": "ReconciliationNoUniqueStrongEvidence",
        "message": (
            "exact good_name queries and complete category scans produced no uniquely "
            "verified product; mapping remains unbound"
        ),
        "at": report["generated_at"],
    }
    base_sequence = len(checkpoint.get("request_ledger") or [])
    appended_ledger = []
    for index, row in enumerate(current_requests, start=1):
        entry = copy.deepcopy(row)
        entry["sequence"] = base_sequence + index
        appended_ledger.append(entry)
    checkpoint.setdefault("request_ledger", []).extend(appended_ledger)
    checkpoint.update({
        "updated_at": now(),
        "status": status,
        "shijiu_product_id": report["shijiu_product_id"],
        "readback": readback,
        "mapping_persisted": bool(readback),
        "prior_terminal_error": prior_error,
        "error": current_error,
        "reconciliation": {
            "mode": report["mode"],
            "status": status,
            "at": report["generated_at"],
            "primary_identity_path": name_evidence["primary_identity_path"],
            "verified_product_ids": report["verified_product_ids"],
            "auxiliary_good_code_product_ids": auxiliary_ids,
            "target_mutations": 0,
        },
    })
    write_json_atomic(checkpoint_path, checkpoint)
    write_json_atomic(reconciliation_report_path, report)

    validation_report = json.loads(validation_report_path.read_text(encoding="utf-8"))
    validation_report.update({
        "generated_at": now(),
        "status": status,
        "shijiu_product_id": report["shijiu_product_id"],
        "mapping_persisted": bool(readback),
        "exact_backend_sku_match_count": 1 if readback else 0,
        "product_identity_readback_policy": name_evidence["primary_identity_path"],
        "good_code_search_role": "auxiliary_only_never_binding",
        "verified_variants": readback["skus"] if readback else [],
        "read_request_count": len(checkpoint["request_ledger"]),
        "error": current_error,
        "reconciliation": {
            "report": "canonical_create_reconciliation_report.json",
            "primary_identity_path": name_evidence["primary_identity_path"],
            "target_mutations": 0,
            "good_code_role": "auxiliary_only_never_binding",
        },
    })
    write_json_atomic(validation_report_path, validation_report)

    candidate_report = json.loads(candidate_report_path.read_text(encoding="utf-8"))
    candidate_report.update({
        "result": status,
        "shijiu_product_id": report["shijiu_product_id"],
        "reconciliation_read_only": True,
        "additional_write_executed": False,
    })
    write_json_atomic(candidate_report_path, candidate_report)
    return report
