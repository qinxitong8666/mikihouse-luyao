from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .shijiu_complex_import import (
    TARGET_CATEGORY_ID,
    UI_READ_INITIAL_BACKOFF_SECONDS,
    UI_READ_MAX_RETRIES,
    UI_TRANSIENT_HTTP_STATUS_CODES,
    _metrics,
)
from .shijiu_import import (
    EXPECTED_SPECIAL_COUNT,
    SOURCE_CODE,
    content_sha256,
    load_mapping_state,
    map_product_to_shijiu,
    now,
)
from .shijiu_live_import import LiveImportError
from .shijiu_production_architecture_verification import FINAL_E2E_PROTECTED_FILES
from .shijiu_staged_media_complete import _configured_product_numbers, _mapped_row_hashes
from .shijiu_staged_media_import import _file_sha256, image_reference_sets, stage_plan
from .shijiu_writer_mutex import mutex_evidence_satisfied


PILOT_MODE = "MIKIHOUSE_PRODUCTION_PILOT_20"
PILOT_CONFIRMATION = "MIKIHOUSE_PRODUCTION_PILOT_20_EXECUTE"
PILOT_PRODUCT_COUNT = 20
VERIFIED_TECHNICAL_PRODUCT = "10-9332-796"
PILOT_PROTECTED_FILES = tuple(dict.fromkeys((
    *FINAL_E2E_PROTECTED_FILES,
    "config/shijiu_richtext_contract.json",
    "deliverables/shijiu_import/richtext_e2e_next_20_frozen_plan.json",
    "deliverables/shijiu_import/richtext_e2e_writer_mutex_audit.json",
    "state/shijiu_richtext_e2e_checkpoint.json",
)))


def stage_summary(item: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "sequence": row["sequence"],
            "key": row["key"],
            "operation": row["operation"],
            "broadcast_count": row["broadcast_count"],
            "good_detail_pics_count": row["detail_pic_count"],
            "new_cos_upload_count": len(row["new_references"]),
        }
        for row in stage_plan(item)
    ]


def validate_frozen_pilot_plan(
    plan: dict[str, Any],
    special: set[str],
    mapping: dict[str, Any],
    *,
    allow_mapped: set[str] | None = None,
) -> list[dict[str, Any]]:
    if plan.get("status") == "STALE_BUSINESS_RULE_CHANGED" or plan.get("must_never_execute"):
        raise LiveImportError("STALE_BUSINESS_RULE_CHANGED: historical 20-product plan must never execute")
    allow_mapped = allow_mapped or set()
    products = plan.get("products") or []
    numbers = [str(row.get("product_number") or "") for row in products]
    if (
        plan.get("source") != SOURCE_CODE
        or plan.get("target") != "SHIJIU"
        or plan.get("fixed_target_category_id") != TARGET_CATEGORY_ID
        or plan.get("product_count") != PILOT_PRODUCT_COUNT
        or len(products) != PILOT_PRODUCT_COUNT
        or len(set(numbers)) != PILOT_PRODUCT_COUNT
        or [row.get("sequence") for row in products] != list(range(1, PILOT_PRODUCT_COUNT + 1))
        or len(special) != EXPECTED_SPECIAL_COUNT
        or set(numbers) & special
        or VERIFIED_TECHNICAL_PRODUCT in numbers
    ):
        raise LiveImportError("frozen 20-product pilot boundary failed")
    for row in products:
        if not row.get("required_stages") or row.get("required_create_count") != 1:
            raise LiveImportError("frozen pilot product lacks a complete stage plan")
        mapped = (mapping.get("products") or {}).get(row["product_number"]) or {}
        if mapped.get("shijiu_product_id") not in (None, "") and row["product_number"] not in allow_mapped:
            raise LiveImportError("frozen pilot contains an already mapped product")
    return copy.deepcopy(products)


def build_pilot_product_selection(
    root: Path,
    master: dict[str, Any],
    special: set[str],
    mapping: dict[str, Any],
    category: dict[str, Any],
    plan_row: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    number = str(plan_row["product_number"])
    product = next(
        (row for row in master.get("products") or [] if row.get("product_number") == number),
        None,
    )
    if not product or not product.get("active") or number in special:
        raise LiveImportError("pilot source product is missing, inactive, or special")
    item = map_product_to_shijiu(product, category, excluded_product_numbers=special)
    if not item.get("publish_ready"):
        raise LiveImportError("pilot source product is not publishable")
    if item["payload_sha256"] != plan_row.get("payload_sha256"):
        raise LiveImportError("pilot source payload drifted from the frozen plan")
    if stage_summary(item) != plan_row.get("required_stages"):
        raise LiveImportError("pilot stage plan drifted from the frozen plan")
    prohibited = _configured_product_numbers(root)
    prohibited.add(VERIFIED_TECHNICAL_PRODUCT)
    prohibited.discard(number)
    refs = image_reference_sets(item)
    metrics = _metrics(product)
    selection = {
        "schema_version": 1,
        "generated_at": now(),
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "mode": PILOT_MODE,
        "pilot_mode": PILOT_MODE,
        "pilot_sequence": plan_row["sequence"],
        "fixed_target_category_id": TARGET_CATEGORY_ID,
        "pdf_special_exclusion_count": len(special),
        "historical_prohibited_product_numbers": sorted(prohibited),
        "maximum_product_create_requests": 1,
        "maximum_product_save_requests_per_runner_invocation": 1,
        "all_resource_preflight_required_before_any_shijiu_write": True,
        "ui_read_retry_policy": {
            "applies_only_to": ["Goods.index", "getFormatInfo"],
            "maximum_retries_after_initial_attempt": UI_READ_MAX_RETRIES,
            "initial_backoff_seconds": UI_READ_INITIAL_BACKOFF_SECONDS,
            "backoff": "exponential",
            "transient_http_status_codes": sorted(UI_TRANSIENT_HTTP_STATUS_CODES),
            "mutation_retry_count": 0,
        },
        "richtext_contract_sha256": _file_sha256(root / "config/shijiu_richtext_contract.json"),
        "protected_frozen_evidence": {
            relative: _file_sha256(root / relative) for relative in PILOT_PROTECTED_FILES
        },
        "protected_existing_mapping_row_hashes": _mapped_row_hashes(mapping),
        "product": {
            "product_number": number,
            "good_name": item["shijiu_payload_preview"]["good_name"],
            "variant_count": metrics["variant_count"],
            "available_variant_count": metrics["available_variant_count"],
            "color_count": metrics["color_count"],
            "size_count": metrics["size_count"],
            "official_image_count": metrics["image_count"],
            "broadcast_count": len(refs["all_broadcast"]),
            "detail_pic_count": len(refs["all_detail"]),
            "source_payload_sha256": item["payload_sha256"],
        },
        "stages": stage_plan(item),
    }
    return item, selection


def initial_pilot_checkpoint(plan: dict[str, Any]) -> dict[str, Any]:
    if plan.get("status") == "STALE_BUSINESS_RULE_CHANGED" or plan.get("must_never_execute"):
        raise LiveImportError("STALE_BUSINESS_RULE_CHANGED: no checkpoint may be created")
    return {
        "schema_version": 1,
        "created_at": now(),
        "updated_at": now(),
        "status": "WAITING_OPERATOR_MUTEX_CONFIRMATION",
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "mode": PILOT_MODE,
        "plan_sha256": content_sha256(plan),
        "product_count": PILOT_PRODUCT_COUNT,
        "next_sequence": 1,
        "products": {
            row["product_number"]: {
                "sequence": row["sequence"],
                "status": "PLANNED",
                "stage_count": len(row["required_stages"]),
                "completed_stage_count": 0,
                "shijiu_product_id": None,
                "error": None,
            }
            for row in plan["products"]
        },
        "stop_reason": "VALID_EXTERNAL_PRODUCTION_WRITER_MUTEX_EVIDENCE_REQUIRED",
        "cross_source_writes": 0,
        "legacy_reference_touched": False,
        "pdf_special_exclusion_count": EXPECTED_SPECIAL_COUNT,
    }


def waiting_operator_report(plan: dict[str, Any], checkpoint: dict[str, Any]) -> dict[str, Any]:
    if plan.get("status") == "STALE_BUSINESS_RULE_CHANGED" or plan.get("must_never_execute"):
        raise LiveImportError("STALE_BUSINESS_RULE_CHANGED: no mutex confirmation may be requested")
    stage_count = sum(len(row.get("required_stages") or []) for row in plan["products"])
    return {
        "schema_version": 1,
        "generated_at": now(),
        "status": "WAITING_OPERATOR_MUTEX_CONFIRMATION",
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "frozen_plan_sha256": content_sha256(plan),
        "product_count": PILOT_PRODUCT_COUNT,
        "planned_stage_count": stage_count,
        "completed_product_count": sum(
            row.get("status") == "COMPLETED" for row in checkpoint["products"].values()
        ),
        "current_next_sequence": checkpoint["next_sequence"],
        "production_write_requests_this_preparation_round": 0,
        "external_mutex_evidence_found": False,
        "operator_action": (
            "Run confirm_shijiu_writer_window.py once after confirming no other project, terminal, "
            "scheduler, or operator is writing the same Shijiu production tenant."
        ),
        "operator_confirmation_required": "我已确认当前SHIJIU正式租户没有其他生产写入任务",
        "operator_confirmation_count": 1,
        "operator_must_create_json_manually": False,
        "execution_authorized_without_evidence": False,
        "cross_source_writes": 0,
        "legacy_reference_touched": False,
        "pdf_special_exclusion_count": EXPECTED_SPECIAL_COUNT,
        "sensitive_values_included": False,
    }


def build_pilot_completion_report(
    plan: dict[str, Any],
    checkpoint: dict[str, Any],
    product_checkpoints: list[dict[str, Any]],
    mapping: dict[str, Any],
) -> dict[str, Any]:
    ledgers = [row for product in product_checkpoints for row in product.get("request_ledger") or []]
    completed = [row for row in product_checkpoints if row.get("status") == "COMPLETED"]
    mappings_complete = all(
        (mapped := (mapping.get("products") or {}).get(row["product_number"]) or {}).get(
            "shijiu_product_id"
        )
        and len(mapped.get("variants") or {}) == int(row["variant_count"])
        and all(
            str(variant.get("backend_sku_code") or "").startswith("MIKI-")
            and variant.get("shijiu_sku_id") is None
            for variant in (mapped.get("variants") or {}).values()
        )
        for row in plan["products"]
    )
    mutex_complete = (
        len(completed) == PILOT_PRODUCT_COUNT
        and all(mutex_evidence_satisfied(row) for row in completed)
    )
    technically_complete = len(completed) == PILOT_PRODUCT_COUNT
    fully_complete = technically_complete and mappings_complete and mutex_complete
    return {
        "schema_version": 1,
        "generated_at": now(),
        "status": "COMPLETED" if fully_complete else checkpoint["status"],
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "product_count": PILOT_PRODUCT_COUNT,
        "successful_product_count": len(completed),
        "sku_count": sum(
            int((row.get("last_verified_state") or {}).get("sku_count") or 0) for row in completed
        ),
        "image_upload_count": sum(row.get("path") == "/v1/cos/upload" for row in ledgers),
        "create_count": sum(
            row.get("path") == "/shopapi/Goods/newAddGood"
            and "create" in str(row.get("operation") or "").casefold()
            for row in ledgers
        ),
        "update_count": sum(
            row.get("path") == "/shopapi/Goods/newAddGood"
            and "update" in str(row.get("operation") or "").casefold()
            for row in ledgers
        ),
        "readback_count": sum(row.get("semantic_operation") == "read" for row in ledgers),
        "failure_count": sum(row.get("outcome") == "ERROR" for row in ledgers),
        "transport_unknown_count": sum(
            "UNKNOWN" in str(row.get("outcome") or "").upper() for row in ledgers
        ),
        "mapping_complete": mappings_complete,
        "cross_source_writes": checkpoint.get("cross_source_writes", 0),
        "legacy_reference_touched": checkpoint.get("legacy_reference_touched", False),
        "pdf_special_exclusion_count": EXPECTED_SPECIAL_COUNT,
        "production_write_mutex_verified_for_every_stage": mutex_complete,
        "sensitive_values_included": False,
    }


def build_remaining_initialization_plan(
    master: dict[str, Any],
    special: set[str],
    mapping: dict[str, Any],
    category: dict[str, Any],
    *,
    batch_size: int = 20,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for product in master.get("products") or []:
        number = str(product.get("product_number") or "")
        if (
            not number
            or number in special
            or not product.get("active")
            or ((mapping.get("products") or {}).get(number) or {}).get("shijiu_product_id")
        ):
            continue
        item = map_product_to_shijiu(product, category, excluded_product_numbers=special)
        if not item.get("publish_ready"):
            continue
        stages = stage_plan(item)
        rows.append({
            "product_number": number,
            "variant_count": len(item.get("source_variants") or []),
            "image_upload_count": len(item.get("image_upload_plan") or []),
            "stage_count": len(stages),
            "stages": [stage["key"] for stage in stages],
        })
    rows.sort(key=lambda row: row["product_number"])
    batches = [
        {
            "batch_number": index // batch_size + 1,
            "product_numbers": [row["product_number"] for row in rows[index:index + batch_size]],
        }
        for index in range(0, len(rows), batch_size)
    ]
    return {
        "schema_version": 1,
        "generated_at": now(),
        "status": "FROZEN_NOT_EXECUTED_REQUIRES_SEPARATE_AUTHORIZATION_AND_MUTEX",
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "fixed_target_category_id": TARGET_CATEGORY_ID,
        "remaining_product_count": len(rows),
        "remaining_variant_count": sum(row["variant_count"] for row in rows),
        "remaining_image_upload_count": sum(row["image_upload_count"] for row in rows),
        "planned_batch_size": batch_size,
        "batch_count": len(batches),
        "batches": batches,
        "products": rows,
        "execution_authorized": False,
        "pdf_special_exclusion_count": len(special),
        "legacy_reference_touched": False,
        "sensitive_values_included": False,
    }
