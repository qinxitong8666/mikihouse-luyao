from __future__ import annotations

import copy
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from .csv_input import read_product_numbers
from .shijiu_import import (
    EXPECTED_LEGACY_REFERENCE_COUNT,
    EXPECTED_SPECIAL_COUNT,
    PDF_SPECIAL_EXCLUDED_REASON,
    SOURCE_CODE,
    content_sha256,
    load_mapping_state,
    now,
    recursively_find_skus,
    response_rows,
    validate_live_mikihouse_category,
    write_json_atomic,
)
from .shijiu_live_import import (
    CREATE_PATH,
    IMAGE_UPLOAD_PATH,
    ContractMismatchError,
    DuplicateRiskError,
    LiveImportError,
    ShijiuLiveClient,
    _product_id_from_value,
    _redacted_response,
    _resolve_payload,
    validate_product_readback,
)


RECOVERY_PRODUCT_NUMBER = "00-1000-028"
RECOVERY_CONFIRMATION = "MIKIHOUSE_00_1000_028_RECOVERY_CREATE_ONCE"
RECOVERY_SCHEMA_VERSION = 1
EXPECTED_FIRST_UPLOAD_COUNT = 12
EXPECTED_NATIVE_REFERENCE_COMMIT = "a36c5eab40bf419562ba03d15c090151698d582a"
EXPECTED_NATIVE_REFERENCE_SHA256 = (
    "1183564685c35f1f79b684077fbb1b1f7ed6bd2b9a46d227edb18ade94c9d016"
)


def _count(response: dict[str, Any]) -> int | None:
    for container in (response, response.get("data")):
        if not isinstance(container, dict):
            continue
        value = container.get("count")
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    value = response.get("count")
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _row_id(row: dict[str, Any]) -> str:
    return str(row.get("id") or row.get("good_id") or row.get("goods_id") or "")


def _row_name(row: dict[str, Any]) -> str:
    return str(row.get("good_name") or row.get("goods_name") or row.get("name") or "").strip()


def _sku_codes(detail: dict[str, Any]) -> set[str]:
    return {
        str(row.get("sku_code") or "").strip()
        for row in recursively_find_skus(detail)
        if str(row.get("sku_code") or "").strip()
    }


def _ids_sha256(ids: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()


def _scan_filter_sets() -> list[dict[str, str]]:
    return [
        {"label": "default", "push": "2", "status": ""},
        {"label": "push_any", "push": "", "status": ""},
        *[
            {"label": f"status_{status}", "push": "2", "status": status}
            for status in ("0", "1", "2")
        ],
        *[
            {"label": f"is_delete_{value}", "push": "2", "status": "", "is_delete": value}
            for value in ("0", "1")
        ],
        *[
            {"label": f"audit_status_{value}", "push": "2", "status": "", "audit_status": value}
            for value in ("0", "1", "2", "3")
        ],
    ]


def complete_category_scan(
    client: ShijiuLiveClient,
    *,
    exact_name: str,
    filters: dict[str, str],
    page_size: int = 200,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    query = {key: value for key, value in filters.items() if key != "label"}
    first = client.search_products(
        page=1, page_size=page_size, good_type=294884, good_name="", sku_code="", **query
    )
    declared = _count(first)
    if declared is None:
        raise ContractMismatchError(f"category scan count missing: {filters['label']}")
    rows = response_rows(first)
    pages = max(1, (declared + page_size - 1) // page_size)
    for page in range(2, pages + 1):
        rows.extend(
            response_rows(
                client.search_products(
                    page=page,
                    page_size=page_size,
                    good_type=294884,
                    good_name="",
                    sku_code="",
                    **query,
                )
            )
        )
    count_end = _count(
        client.search_products(
            page=1, page_size=1, good_type=294884, good_name="", sku_code="", **query
        )
    )
    ids = [_row_id(row) for row in rows]
    complete = (
        count_end == declared
        and len(rows) == declared
        and all(ids)
        and len(set(ids)) == len(ids)
    )
    exact_rows = [row for row in rows if _row_name(row) == exact_name]
    summary = {
        "label": filters["label"],
        "filters": query,
        "count_start": declared,
        "count_end": count_end,
        "row_count": len(rows),
        "unique_id_count": len(set(ids)),
        "id_set_sha256": _ids_sha256(ids),
        "exact_name_match_ids": [_row_id(row) for row in exact_rows],
        "complete": complete,
    }
    if not complete:
        raise ContractMismatchError(f"incomplete/stale category scan: {summary}")
    return summary, exact_rows


def exact_identity_queries(
    client: ShijiuLiveClient,
    *,
    good_name: str,
    backend_sku_code: str,
) -> dict[str, Any]:
    sku_matches: dict[str, dict[str, Any]] = {}
    exact_name_matches: dict[str, dict[str, Any]] = {}
    query_count = 0
    for good_type in (294884, ""):
        for push in ("", "0", "1", "2"):
            for status in ("", "0", "1", "2"):
                response = client.search_products(
                    backend_sku_code,
                    good_type=good_type,
                    push=push,
                    status=status,
                    page=1,
                    page_size=100,
                )
                query_count += 1
                for row in response_rows(response):
                    if _row_id(row):
                        sku_matches[_row_id(row)] = row
    for good_type in (294884, ""):
        response = client.search_products(
            "",
            good_name=good_name,
            good_type=good_type,
            push="",
            status="",
            page=1,
            page_size=100,
        )
        query_count += 1
        for row in response_rows(response):
            if _row_id(row) and _row_name(row) == good_name:
                exact_name_matches[_row_id(row)] = row
    return {
        "query_count": query_count,
        "exact_sku_result_ids": sorted(sku_matches),
        "exact_name_result_ids": sorted(exact_name_matches),
        "sku_rows": list(sku_matches.values()),
        "name_rows": list(exact_name_matches.values()),
    }


def load_recovery_inputs(
    previews_path: Path,
    original_checkpoint_path: Path,
    mapping_path: Path,
    special_path: Path,
    native_contract_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], set[str], dict[str, Any]]:
    previews = json.loads(previews_path.read_text(encoding="utf-8"))
    matches = [
        item
        for item in previews.get("payloads") or []
        if item.get("product_number") == RECOVERY_PRODUCT_NUMBER
    ]
    if len(matches) != 1:
        raise LiveImportError("frozen preview does not contain exactly one recovery product")
    item = copy.deepcopy(matches[0])
    special = set(read_product_numbers(special_path))
    if len(special) != EXPECTED_SPECIAL_COUNT or RECOVERY_PRODUCT_NUMBER in special:
        raise LiveImportError(f"{PDF_SPECIAL_EXCLUDED_REASON}: recovery boundary failed")
    original = json.loads(original_checkpoint_path.read_text(encoding="utf-8"))
    if original.get("source") != SOURCE_CODE or original.get("target") != "SHIJIU":
        raise LiveImportError("invalid original first-batch checkpoint provider boundary")
    record = original.get("records", {}).get(RECOVERY_PRODUCT_NUMBER) or {}
    if record.get("shijiu_product_id") not in (None, ""):
        raise DuplicateRiskError("original checkpoint already has a Shijiu product ID")
    uploads = record.get("image_uploads") or {}
    if len(uploads) != EXPECTED_FIRST_UPLOAD_COUNT or any(
        row.get("status") != "UPLOADED" or not str(row.get("target_url") or "").startswith("https://cdn0.19mini.com/")
        for row in uploads.values()
    ):
        raise LiveImportError("the 12 prior Shijiu/COS uploads are incomplete or invalid")
    if set(uploads) != {row["upload_reference"] for row in item["image_upload_plan"]}:
        raise LiveImportError("checkpoint uploads differ from the frozen image plan")
    mapping = load_mapping_state(mapping_path)
    mapping_row = mapping["products"][RECOVERY_PRODUCT_NUMBER]
    if mapping_row.get("shijiu_product_id") not in (None, "") or any(
        variant.get("shijiu_sku_id") not in (None, "")
        for variant in mapping_row["variants"].values()
    ):
        raise DuplicateRiskError("mapping state is already bound for the recovery product")
    contract = json.loads(native_contract_path.read_text(encoding="utf-8"))
    original_payload = _resolve_payload(item, uploads)
    payload = copy.deepcopy(original_payload)
    payload["state"] = contract.get("state")
    payload["is_shelf"] = contract.get("is_shelf")
    if (
        contract.get("target") != "SHIJIU"
        or contract.get("reference_commit") != EXPECTED_NATIVE_REFERENCE_COMMIT
        or contract.get("reference_file_sha256") != EXPECTED_NATIVE_REFERENCE_SHA256
        or contract.get("create_endpoint") != CREATE_PATH
        or payload["state"] != "1"
        or payload["is_shelf"] != 0
        or list(payload) != contract.get("product_fields")
        or list(payload["sku_info"][0]) != contract.get("sku_fields")
        or list(payload["spec_name"][0]) != contract.get("spec_fields")
    ):
        raise LiveImportError("recovery payload differs from the audited native create contract")
    if original_payload.get("state") != "0" or original_payload.get("is_shelf") != 0:
        raise LiveImportError("the failed-attempt payload no longer has state=0/is_shelf=0")
    if any(
        original_payload[key] != payload[key]
        for key in payload
        if key != "state"
    ):
        raise LiveImportError("recovery changed fields other than the audited state semantic")
    if content_sha256(item["shijiu_payload_preview"]) != item.get("payload_sha256"):
        raise LiveImportError("frozen source payload hash mismatch")
    return item, record, payload, special, contract


def initial_recovery_checkpoint(
    item: dict[str, Any], record: dict[str, Any], payload: dict[str, Any], contract: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "scope": {"product_numbers": [RECOVERY_PRODUCT_NUMBER], "maximum_create_requests": 1},
        "created_at": now(),
        "updated_at": now(),
        "state": "READY_FOR_READ_ONLY_RESIDUAL_SCAN",
        "recovery_create_attempts": 0,
        "image_upload_requests": 0,
        "legacy_cleanup_requests": 0,
        "subsequent_batch_product_requests": 0,
        "prior_create_attempt": {
            "at": record.get("create_intent_at"),
            "response": record.get("create_response"),
            "confirmed_product_id": record.get("shijiu_product_id"),
        },
        "reused_image_count": len(record["image_uploads"]),
        "reused_image_target_urls_sha256": content_sha256([
            row["target_url"] for row in record["image_uploads"].values()
        ]),
        "source_payload_sha256": item["payload_sha256"],
        "recovery_payload_sha256": content_sha256(payload),
        "native_contract": {
            "reference_repository": contract["reference_repository"],
            "reference_commit": contract["reference_commit"],
            "reference_file_sha256": contract["reference_file_sha256"],
            "state": "1",
            "is_shelf": 0,
        },
        "residual_scan": None,
        "create_intent_at": None,
        "create_response": None,
        "shijiu_product_id": None,
        "readback": None,
        "mapping_persisted": False,
        "error": None,
        "request_ledger": [],
    }


def _save(path: Path, checkpoint: dict[str, Any]) -> None:
    checkpoint["updated_at"] = now()
    write_json_atomic(path, checkpoint)


def load_or_create_recovery_checkpoint(
    path: Path,
    item: dict[str, Any],
    record: dict[str, Any],
    payload: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    if not path.exists():
        result = initial_recovery_checkpoint(item, record, payload, contract)
        _save(path, result)
        return result
    result = json.loads(path.read_text(encoding="utf-8"))
    if (
        result.get("source") != SOURCE_CODE
        or result.get("target") != "SHIJIU"
        or result.get("scope") != {"product_numbers": [RECOVERY_PRODUCT_NUMBER], "maximum_create_requests": 1}
        or result.get("source_payload_sha256") != item["payload_sha256"]
        or result.get("recovery_payload_sha256") != content_sha256(payload)
    ):
        raise LiveImportError("recovery checkpoint identity or payload drift")
    return result


def prove_no_residual(
    client: ShijiuLiveClient,
    item: dict[str, Any],
    original_record: dict[str, Any],
    mapping_path: Path,
) -> dict[str, Any]:
    validate_live_mikihouse_category(
        item["target_category"], client.categories()
    )
    expected_code = item["source_variants"][0]["backend_sku_code"]
    identity = exact_identity_queries(
        client,
        good_name=item["shijiu_payload_preview"]["good_name"],
        backend_sku_code=expected_code,
    )
    scans = []
    name_rows = identity.pop("name_rows")
    sku_rows = identity.pop("sku_rows")
    exact_rows: dict[str, dict[str, Any]] = {
        _row_id(row): row for row in name_rows + sku_rows if _row_id(row)
    }
    for filters in _scan_filter_sets():
        summary, matches = complete_category_scan(
            client,
            exact_name=item["shijiu_payload_preview"]["good_name"],
            filters=filters,
        )
        scans.append(summary)
        for row in matches:
            exact_rows[_row_id(row)] = row
    candidate_details = []
    expected_codes = {variant["backend_sku_code"] for variant in item["source_variants"]}
    for product_id in sorted(exact_rows):
        detail = client.product_detail(product_id)
        codes = _sku_codes(detail)
        candidate_details.append({
            "product_id": product_id,
            "miki_sku_codes": sorted(code for code in codes if code.startswith("MIKI-")),
            "exact_expected_sku_set": codes == expected_codes,
        })
    mapping = load_mapping_state(mapping_path)["products"][RECOVERY_PRODUCT_NUMBER]
    default_scan = next(row for row in scans if row["label"] == "default")
    reasons = []
    if identity["exact_sku_result_ids"]:
        reasons.append("exact SKU query returned rows")
    if exact_rows:
        reasons.append("exact source-name rows exist")
    if any(row["exact_expected_sku_set"] for row in candidate_details):
        reasons.append("a candidate detail has the complete expected MIKI SKU set")
    if default_scan["count_start"] != EXPECTED_LEGACY_REFERENCE_COUNT:
        reasons.append(
            f"default category count is {default_scan['count_start']}, expected legacy baseline 286"
        )
    if original_record.get("shijiu_product_id") not in (None, ""):
        reasons.append("original checkpoint is already bound")
    if mapping.get("shijiu_product_id") not in (None, ""):
        reasons.append("mapping state is already bound")
    passed = not reasons and all(row["complete"] for row in scans)
    return {
        "performed_at": now(),
        "mode": "READ_ONLY",
        "category_id": 294884,
        "baseline_legacy_count": EXPECTED_LEGACY_REFERENCE_COUNT,
        "identity_queries": identity,
        "category_scans": scans,
        "exact_name_candidate_details": candidate_details,
        "original_create_response": original_record.get("create_response"),
        "original_checkpoint_product_id": original_record.get("shijiu_product_id"),
        "mapping_product_id": mapping.get("shijiu_product_id"),
        "absence_proof_reasons": reasons,
        "legacy_rows_modified": 0,
        "legacy_mappings_created": 0,
        "passed": passed,
    }


def discover_created_product(
    client: ShijiuLiveClient,
    item: dict[str, Any],
    create_response: dict[str, Any],
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    expected_codes = {variant["backend_sku_code"] for variant in item["source_variants"]}
    response_id = _product_id_from_value(create_response)
    observations = []
    verified: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for delay in (0, 2, 5, 10, 20):
        if delay:
            time.sleep(delay)
        identity = exact_identity_queries(
            client,
            good_name=item["shijiu_payload_preview"]["good_name"],
            backend_sku_code=item["source_variants"][0]["backend_sku_code"],
        )
        _, category_matches = complete_category_scan(
            client,
            exact_name=item["shijiu_payload_preview"]["good_name"],
            filters={"label": "post_create_default", "push": "2", "status": ""},
        )
        candidate_rows = {
            _row_id(row): row
            for row in identity["name_rows"] + identity["sku_rows"] + category_matches
            if _row_id(row)
        }
        if response_id:
            candidate_rows.setdefault(response_id, {"id": response_id})
        round_result = {
            "delay_seconds": delay,
            "exact_sku_result_ids": identity["exact_sku_result_ids"],
            "exact_name_result_ids": identity["exact_name_result_ids"],
            "candidate_ids": sorted(candidate_rows),
            "exact_expected_sku_set_ids": [],
        }
        for product_id, row in candidate_rows.items():
            detail = client.product_detail(product_id)
            if _sku_codes(detail) == expected_codes:
                verified[product_id] = (row, detail)
                round_result["exact_expected_sku_set_ids"].append(product_id)
        observations.append(round_result)
        if len(verified) == 1:
            product_id = next(iter(verified))
            row, detail = verified[product_id]
            return product_id, row, {
                "search_rounds": observations,
                "detail": detail,
                "unique_exact_identity": True,
            }
        if len(verified) > 1:
            break
    raise ContractMismatchError(
        f"post-create identity is not unique: exact verified IDs={sorted(verified)}"
    )


def persist_mapping_after_recovery(
    path: Path,
    item: dict[str, Any],
    readback: dict[str, Any],
    payload_hash: str,
) -> None:
    state = load_mapping_state(path)
    row = state["products"][RECOVERY_PRODUCT_NUMBER]
    if row.get("shijiu_product_id") not in (None, "", readback["shijiu_product_id"]):
        raise DuplicateRiskError("refusing to replace an existing product mapping")
    row.update({
        "shijiu_product_id": readback["shijiu_product_id"],
        "match_method": "controlled_recovery_post_create_multi_path_readback",
        "target_category_id": 294884,
        "target_active": False,
        "last_payload_sha256": payload_hash,
        "last_verified_at": readback["verified_at"],
    })
    for result in readback["skus"]:
        variant = row["variants"][result["source_variant_sku"]]
        if variant.get("shijiu_sku_id") not in (None, "", result["shijiu_sku_id"]):
            raise DuplicateRiskError("refusing to replace an existing SKU mapping")
        variant.update({
            "shijiu_sku_id": result["shijiu_sku_id"],
            "match_method": "controlled_recovery_post_create_multi_path_readback",
            "last_verified_at": readback["verified_at"],
        })
    state["updated_at"] = now()
    write_json_atomic(path, state)


class FirstProductRecoveryRunner:
    def __init__(
        self,
        client: ShijiuLiveClient,
        item: dict[str, Any],
        original_record: dict[str, Any],
        payload: dict[str, Any],
        contract: dict[str, Any],
        checkpoint_path: Path,
        mapping_path: Path,
        report_path: Path,
        residual_report_path: Path,
        readback_path: Path,
        *,
        confirmation: str,
    ) -> None:
        if confirmation != RECOVERY_CONFIRMATION:
            raise LiveImportError("exact one-product recovery confirmation is missing")
        self.client = client
        self.item = item
        self.original_record = original_record
        self.payload = payload
        self.contract = contract
        self.checkpoint_path = checkpoint_path
        self.mapping_path = mapping_path
        self.report_path = report_path
        self.residual_report_path = residual_report_path
        self.readback_path = readback_path
        self.confirmation = confirmation
        self.checkpoint = load_or_create_recovery_checkpoint(
            checkpoint_path, item, original_record, payload, contract
        )
        self._request_cursor = 0

    def _persist(self) -> None:
        self.checkpoint["request_ledger"].extend(
            copy.deepcopy(self.client.requests[self._request_cursor:])
        )
        self._request_cursor = len(self.client.requests)
        if any(row["path"] == IMAGE_UPLOAD_PATH for row in self.checkpoint["request_ledger"]):
            raise LiveImportError("recovery invariant violated: image upload request observed")
        creates = sum(row["path"] == CREATE_PATH for row in self.checkpoint["request_ledger"])
        if creates > 1:
            raise LiveImportError("recovery invariant violated: more than one create request")
        _save(self.checkpoint_path, self.checkpoint)
        self._write_reports()

    def _write_reports(self) -> None:
        ledger = self.checkpoint["request_ledger"]
        report = {
            "schema_version": RECOVERY_SCHEMA_VERSION,
            "generated_at": now(),
            "source": SOURCE_CODE,
            "target": "SHIJIU",
            "scope": self.checkpoint["scope"],
            "state": self.checkpoint["state"],
            "error": self.checkpoint.get("error"),
            "state_semantics": {
                "failed_attempt": {"state": "0", "is_shelf": 0},
                "audited_native_recovery": {"state": "1", "is_shelf": 0},
                "visibility": "OFF_SHELF_INVISIBLE",
            },
            "residual_absence_proven": bool(
                (self.checkpoint.get("residual_scan") or {}).get("passed")
            ),
            "images": {
                "prior_cos_images_reused": self.checkpoint["reused_image_count"],
                "new_upload_requests": 0,
            },
            "requests": {
                "total": len(ledger),
                "read": sum(row["semantic_operation"] == "read" for row in ledger),
                "write": sum(row["semantic_operation"] == "write" for row in ledger),
                "product_create": sum(row["path"] == CREATE_PATH for row in ledger),
                "image_upload": 0,
                "legacy_cleanup": 0,
                "later_batch_products": 0,
            },
            "recovery_create_attempts": self.checkpoint["recovery_create_attempts"],
            "create_response": self.checkpoint.get("create_response"),
            "shijiu_product_id": self.checkpoint.get("shijiu_product_id"),
            "mapping_persisted": self.checkpoint.get("mapping_persisted"),
            "readback_passed": bool((self.checkpoint.get("readback") or {}).get("passed")),
            "post_recovery_forensics": self.checkpoint.get("post_recovery_forensics"),
            "legacy_products_modified": 0,
            "legacy_cleanup_executed": False,
            "subsequent_19_products_processed": 0,
            "checkpoint": "state/shijiu_first_product_recovery_checkpoint.json",
            "residual_report": "deliverables/shijiu_import/first_product_residual_scan.json",
            "readback_report": "deliverables/shijiu_import/first_product_recovery_readback.json",
            "post_recovery_forensics_report": (
                "deliverables/shijiu_import/first_product_recovery_forensics.json"
            ),
        }
        write_json_atomic(self.report_path, report)
        write_json_atomic(
            self.readback_path,
            {
                "schema_version": RECOVERY_SCHEMA_VERSION,
                "generated_at": now(),
                "source": SOURCE_CODE,
                "target": "SHIJIU",
                "product_number": RECOVERY_PRODUCT_NUMBER,
                "state": self.checkpoint["state"],
                "result": self.checkpoint.get("readback"),
                "passed": bool((self.checkpoint.get("readback") or {}).get("passed")),
                "unavailable_reason": (
                    None
                    if self.checkpoint.get("readback")
                    else "no unique product_id was exposed by response or list/SKU/name discovery"
                ),
                "post_recovery_forensics": self.checkpoint.get(
                    "post_recovery_forensics"
                ),
            },
        )

    def _stop(self, error: Exception) -> None:
        self.checkpoint["state"] = "STOPPED_ON_RECOVERY_ERROR"
        self.checkpoint["error"] = {
            "type": type(error).__name__, "message": str(error), "at": now()
        }
        self._persist()

    def run_residual_only(self) -> dict[str, Any]:
        if self.checkpoint["state"] != "READY_FOR_READ_ONLY_RESIDUAL_SCAN":
            raise LiveImportError("residual-only check requires a fresh recovery checkpoint")
        try:
            residual = prove_no_residual(
                self.client, self.item, self.original_record, self.mapping_path
            )
            self.checkpoint["residual_scan"] = residual
            write_json_atomic(self.residual_report_path, residual)
            if not residual["passed"]:
                raise DuplicateRiskError(
                    f"residual absence was not proven: {residual['absence_proof_reasons']}"
                )
            self.checkpoint["state"] = "RESIDUAL_ABSENCE_PROVEN"
            self._persist()
        except Exception as error:
            self._stop(error)
            raise
        return json.loads(self.report_path.read_text(encoding="utf-8"))

    def run_post_recovery_forensics(self, output_path: Path) -> dict[str, Any]:
        """Perform a read-only terminal audit after the one create budget is consumed."""
        if (
            self.checkpoint["state"] != "STOPPED_ON_RECOVERY_ERROR"
            or self.checkpoint["recovery_create_attempts"] != 1
        ):
            raise LiveImportError(
                "post-recovery forensics requires a stopped checkpoint with one consumed create"
            )
        request_start = len(self.client.requests)
        residual = prove_no_residual(
            self.client, self.item, self.original_record, self.mapping_path
        )
        requests = self.client.requests[request_start:]
        observable_product_found = bool(
            residual["identity_queries"]["exact_sku_result_ids"]
            or residual["identity_queries"]["exact_name_result_ids"]
            or any(
                row["exact_expected_sku_set"]
                for row in residual["exact_name_candidate_details"]
            )
        )
        result = {
            "schema_version": RECOVERY_SCHEMA_VERSION,
            "performed_at": now(),
            "mode": "POST_RECOVERY_READ_ONLY_FORENSICS",
            "source": SOURCE_CODE,
            "target": "SHIJIU",
            "product_number": RECOVERY_PRODUCT_NUMBER,
            "create_attempts_already_consumed": 1,
            "create_response": self.checkpoint.get("create_response"),
            "terminal_error": self.checkpoint.get("error"),
            "observable_product_found": observable_product_found,
            "residual_absence_still_proven": residual["passed"],
            "multi_path": {
                "product_list_and_exact_sku_queries": residual["identity_queries"],
                "complete_category_scans": residual["category_scans"],
                "getFormatInfo_candidate_details": residual[
                    "exact_name_candidate_details"
                ],
                "getFormatInfo_not_possible_reason": (
                    None
                    if residual["exact_name_candidate_details"]
                    else "no product_id candidate was returned by list/SKU/name discovery"
                ),
            },
            "requests": {
                "read": sum(row["semantic_operation"] == "read" for row in requests),
                "write": sum(row["semantic_operation"] == "write" for row in requests),
                "image_upload": sum(row["path"] == IMAGE_UPLOAD_PATH for row in requests),
                "product_create": sum(row["path"] == CREATE_PATH for row in requests),
            },
            "mapping_persisted": False,
            "subsequent_19_products_processed": 0,
            "legacy_products_modified": 0,
            "conclusion": (
                "the single recovery request returned success without an ID, but the product "
                "remains unobservable and therefore cannot be treated as created"
            ),
        }
        write_json_atomic(output_path, result)
        self.checkpoint["post_recovery_forensics"] = {
            "performed_at": result["performed_at"],
            "observable_product_found": result["observable_product_found"],
            "residual_absence_still_proven": result["residual_absence_still_proven"],
            "requests": result["requests"],
            "report": "deliverables/shijiu_import/first_product_recovery_forensics.json",
        }
        self._persist()
        return result

    def run(self) -> dict[str, Any]:
        if self.checkpoint["state"] in {"RECOVERY_READBACK_VERIFIED", "STOPPED_ON_RECOVERY_ERROR"}:
            raise LiveImportError("recovery checkpoint is terminal; no additional create is permitted")
        if self.checkpoint["recovery_create_attempts"] != 0:
            raise DuplicateRiskError("the one-request recovery mutation budget is already consumed")
        try:
            if self.checkpoint["state"] == "READY_FOR_READ_ONLY_RESIDUAL_SCAN":
                residual = prove_no_residual(
                    self.client, self.item, self.original_record, self.mapping_path
                )
                self.checkpoint["residual_scan"] = residual
                write_json_atomic(self.residual_report_path, residual)
                if not residual["passed"]:
                    raise DuplicateRiskError(
                        f"residual absence was not proven: {residual['absence_proof_reasons']}"
                    )
                self.checkpoint["state"] = "RESIDUAL_ABSENCE_PROVEN"
                self._persist()
            elif self.checkpoint["state"] != "RESIDUAL_ABSENCE_PROVEN":
                raise LiveImportError(
                    f"recovery cannot create from checkpoint state {self.checkpoint['state']}"
                )
            self.checkpoint["state"] = "RECOVERY_CREATE_INTENT_PERSISTED"
            self.checkpoint["create_intent_at"] = now()
            self.checkpoint["recovery_create_attempts"] = 1
            self._persist()
            try:
                response = self.client.create_product(
                    copy.deepcopy(self.payload), confirmation=self.confirmation
                )
            except Exception:
                self.checkpoint["state"] = "RECOVERY_CREATE_RESULT_UNKNOWN"
                self._persist()
                raise
            self.checkpoint["create_response"] = _redacted_response(response)
            self.checkpoint["state"] = "RECOVERY_CREATE_RESPONSE_RECEIVED"
            self._persist()
            product_id, list_row, discovery = discover_created_product(
                self.client, self.item, response
            )
            self.checkpoint["shijiu_product_id"] = product_id
            self.checkpoint["state"] = "RECOVERY_IDENTITY_CONFIRMED"
            self._persist()
            readback = validate_product_readback(
                self.item,
                self.payload,
                product_id,
                discovery["detail"],
                create_response=response,
                list_row=list_row,
                expected_state="1",
            )
            readback["multi_path"] = {
                "goods_index": {
                    "product_id": product_id,
                    "category_id": list_row.get("good_type"),
                    "state": list_row.get("state"),
                    "is_shelf": list_row.get("is_shelf"),
                },
                "exact_sku_identity": sorted(_sku_codes(discovery["detail"])),
                "getFormatInfo": {
                    "code": discovery["detail"].get("code"),
                    "product_id": product_id,
                    "sku_count": len(recursively_find_skus(discovery["detail"])),
                },
                "search_rounds": discovery["search_rounds"],
            }
            persist_mapping_after_recovery(
                self.mapping_path, self.item, readback, content_sha256(self.payload)
            )
            self.checkpoint["readback"] = readback
            self.checkpoint["mapping_persisted"] = True
            self.checkpoint["state"] = "RECOVERY_READBACK_VERIFIED"
            self._persist()
        except Exception as error:
            self._stop(error)
            raise
        return json.loads(self.report_path.read_text(encoding="utf-8"))
