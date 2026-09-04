from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from .shijiu_canonical_create import load_verified_browser_credentials
from .shijiu_complex_import import TARGET_CATEGORY_ID, ComplexLiveBatchRunner, UiContextReadClient, _metrics
from .shijiu_complexity_bisection import (
    ALL_PREVIOUS_CREATE_PRODUCTS,
    payload_complexity_metrics,
)
from .shijiu_import import (
    EXPECTED_SPECIAL_COUNT,
    SOURCE_CODE,
    content_sha256,
    map_product_to_shijiu,
    now,
)
from .shijiu_live_import import LiveImportError, ShijiuLiveClient, _resolve_payload


HIGH_SKU_PRODUCT_NUMBER = "63-6602-492"
HIGH_SKU_MODE = "HIGH_SKU_14_SINGLE_REAL_IMPORT_VALIDATION"
HIGH_SKU_WRITE_CONFIRMATION = "MIKIHOUSE_HIGH_SKU_14_SINGLE_REAL_IMPORT"
FROZEN_IMAGE_PROBE = "00-4000-057"
HARD_PROHIBITED_PRODUCTS = {*ALL_PREVIOUS_CREATE_PRODUCTS, FROZEN_IMAGE_PROBE}


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _flatten_payload_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "business_payload_utf8_byte_count": metrics["business_payload_utf8_byte_count"],
        "wire_body_utf8_byte_count": metrics["wire_body_utf8_byte_count"],
        "sku_count": metrics["sku_info_count"],
        "spec_dimension_count": metrics["spec_dimension_count"],
        "spec_option_count_total": metrics["spec_option_count_total"],
        "broadcast_character_count": metrics["broadcast"]["character_count"],
        "broadcast_utf8_byte_count": metrics["broadcast"]["utf8_byte_count"],
        "broadcast_url_count": metrics["broadcast"]["url_count"],
        "broadcast_image_count": metrics["broadcast"]["url_count"],
        "good_detail_pics_character_count": metrics["good_detail_pics"]["character_count"],
        "good_detail_pics_utf8_byte_count": metrics["good_detail_pics"]["utf8_byte_count"],
        "good_detail_pics_url_count": metrics["good_detail_pics"]["url_count"],
        "good_detail_pics_image_count": metrics["good_detail_pics"]["url_count"],
        "good_details_character_count": metrics["good_details"]["character_count"],
        "good_details_utf8_byte_count": metrics["good_details"]["utf8_byte_count"],
        "good_details_url_count": metrics["good_details"]["embedded_url_count"],
        "good_details_image_count": metrics["good_details"]["image_tag_count"],
    }


def build_historical_payload_rows(
    root: Path,
    master: dict[str, Any],
    special: set[str],
    category: dict[str, Any],
    *,
    token: str,
    secret: str,
) -> list[dict[str, Any]]:
    by_number = {
        str(product.get("product_number") or ""): product
        for product in master.get("products") or []
    }
    canonical = json.loads(
        (root / "state/shijiu_canonical_create_checkpoint.json").read_text(encoding="utf-8")
    )
    complex_checkpoint = json.loads(
        (root / "state/shijiu_complex_live_batch_checkpoint.json").read_text(encoding="utf-8")
    )
    bisection = json.loads(
        (root / "state/shijiu_complexity_bisection_checkpoint.json").read_text(encoding="utf-8")
    )
    references = [
        (
            "36-2001-572_SUCCESS",
            "36-2001-572",
            "CREATE_PERSISTED_AND_STRONGLY_READ_BACK",
            canonical.get("image_uploads") or {},
            canonical.get("resolved_payload_sha256"),
        ),
        (
            "13-9310-490_FAILED",
            "13-9310-490",
            "CREATE_RESPONSE_RECEIVED_NOT_OBSERVED_PERSISTED",
            complex_checkpoint["records"]["13-9310-490"].get("image_uploads") or {},
            complex_checkpoint["records"]["13-9310-490"].get("resolved_payload_sha256"),
        ),
        (
            "00-4000-057_FAILED",
            "00-4000-057",
            "CREATE_RESPONSE_RECEIVED_NOT_OBSERVED_PERSISTED",
            bisection["records"]["00-4000-057"].get("image_uploads") or {},
            bisection["records"]["00-4000-057"].get("resolved_payload_sha256"),
        ),
    ]
    rows = []
    for label, number, outcome, uploads, expected_hash in references:
        product = by_number.get(number)
        if not product:
            raise LiveImportError(f"historical payload source missing: {number}")
        item = map_product_to_shijiu(product, category, excluded_product_numbers=special)
        payload = _resolve_payload(item, uploads)
        if content_sha256(payload) != expected_hash:
            raise LiveImportError(f"historical resolved payload hash drift: {number}")
        rows.append({
            "label": label,
            "product_number": number,
            "outcome": outcome,
            "metrics": _flatten_payload_metrics(
                payload_complexity_metrics(payload, token=token, secret=secret)
            ),
        })
    return rows


def build_high_sku_selection(
    root: Path,
    master: dict[str, Any],
    special: set[str],
    category: dict[str, Any],
    capacity_audit_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if len(special) != EXPECTED_SPECIAL_COUNT or HIGH_SKU_PRODUCT_NUMBER in special:
        raise LiveImportError("permanent PDF special exclusion boundary failed")
    old_selection_path = root / "config/shijiu_complexity_bisection_batch.json"
    old_checkpoint_path = root / "state/shijiu_complexity_bisection_checkpoint.json"
    old_report_path = root / "deliverables/shijiu_import/complexity_bisection_report.json"
    complex_checkpoint_path = root / "state/shijiu_complex_live_batch_checkpoint.json"
    old_selection = json.loads(old_selection_path.read_text(encoding="utf-8"))
    old_checkpoint = json.loads(old_checkpoint_path.read_text(encoding="utf-8"))
    old_record = (old_checkpoint.get("records") or {}).get(HIGH_SKU_PRODUCT_NUMBER) or {}
    if (
        [row.get("product_number") for row in old_selection.get("products") or []]
        != [FROZEN_IMAGE_PROBE, HIGH_SKU_PRODUCT_NUMBER]
        or old_checkpoint.get("status") != "STOPPED_ON_FIRST_ERROR"
        or old_record.get("state") != "PLANNED"
        or old_record.get("create_attempts") != 0
        or old_record.get("image_uploads") != {}
    ):
        raise LiveImportError("prior bisection is not in the required permanently frozen state")
    audit = json.loads(capacity_audit_path.read_text(encoding="utf-8"))
    if (
        audit.get("status") != "COMPLETED_READ_ONLY"
        or audit.get("target_mutation_requests_sent") != 0
        or not (audit.get("scope") or {}).get(
            "deterministic_evenly_spaced_page_sample_completed"
        )
    ):
        raise LiveImportError("completed strict read-only capacity audit is required")
    product = next(
        (
            row
            for row in master.get("products") or []
            if row.get("product_number") == HIGH_SKU_PRODUCT_NUMBER
        ),
        None,
    )
    if not product or not product.get("active"):
        raise LiveImportError("fixed high-SKU probe is missing or inactive")
    item = map_product_to_shijiu(product, category, excluded_product_numbers=special)
    metrics = _metrics(product)
    detail_count = len(
        [
            value
            for value in str(item["shijiu_payload_preview"].get("good_detail_pics") or "").split(",")
            if value
        ]
    )
    if (
        not item.get("publish_ready")
        or metrics["variant_count"] != 14
        or metrics["available_variant_count"] != 14
        or metrics["image_count"] != 6
        or detail_count != 4
    ):
        raise LiveImportError("fixed high-SKU probe complexity or publishability drift")
    planned_metrics = payload_complexity_metrics(item["shijiu_payload_preview"])
    protected = {
        "original_complex_batch_checkpoint_sha256": _file_sha256(complex_checkpoint_path),
        "prior_bisection_selection_sha256": _file_sha256(old_selection_path),
        "prior_bisection_checkpoint_sha256": _file_sha256(old_checkpoint_path),
        "prior_bisection_report_sha256": _file_sha256(old_report_path),
    }
    selection = {
        "schema_version": 1,
        "generated_at": now(),
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "mode": HIGH_SKU_MODE,
        "selection_policy": "fixed previously-planned low-image high-SKU candidate reauthorized as an independent one-product batch",
        "independent_of_prior_serial_gate": True,
        "prior_bisection_remains_frozen": True,
        "original_complex_five_remain_frozen": True,
        "fixed_target_category_id": TARGET_CATEGORY_ID,
        "pdf_special_exclusion_count": len(special),
        "maximum_product_create_requests": 1,
        "hard_prohibited_products": sorted(HARD_PROHIBITED_PRODUCTS),
        "capacity_audit_sha256": _file_sha256(capacity_audit_path),
        "protected_frozen_evidence": protected,
        "products": [{
            "sequence": 1,
            "role": "LOW_IMAGE_HIGH_SKU_14_VARIANT_PROBE",
            "product_number": HIGH_SKU_PRODUCT_NUMBER,
            "good_name": item["shijiu_payload_preview"]["good_name"],
            "variant_count": metrics["variant_count"],
            "available_variant_count": metrics["available_variant_count"],
            "color_count": metrics["color_count"],
            "size_count": metrics["size_count"],
            "image_count": metrics["image_count"],
            "detail_image_count": detail_count,
            "planned_business_payload_utf8_byte_count": planned_metrics[
                "business_payload_utf8_byte_count"
            ],
            "payload_sha256": item["payload_sha256"],
        }],
    }
    return [copy.deepcopy(item)], selection


def load_frozen_high_sku_item(
    root: Path,
    master: dict[str, Any],
    special: set[str],
    category: dict[str, Any],
    selection: dict[str, Any],
    capacity_audit_path: Path,
) -> list[dict[str, Any]]:
    items, expected = build_high_sku_selection(
        root, master, special, category, capacity_audit_path
    )
    stable_keys = (
        "source",
        "target",
        "mode",
        "independent_of_prior_serial_gate",
        "prior_bisection_remains_frozen",
        "original_complex_five_remain_frozen",
        "fixed_target_category_id",
        "pdf_special_exclusion_count",
        "maximum_product_create_requests",
        "hard_prohibited_products",
        "capacity_audit_sha256",
        "protected_frozen_evidence",
        "products",
    )
    if any(selection.get(key) != expected.get(key) for key in stable_keys):
        raise LiveImportError("frozen high-SKU single-product selection drift")
    return items


def make_high_sku_clients(
    private_dir: Path,
    canonical_contract_path: Path,
) -> tuple[ShijiuLiveClient, UiContextReadClient, dict[str, Any]]:
    token, secret, evidence = load_verified_browser_credentials(
        private_dir, canonical_contract_path
    )
    client = ShijiuLiveClient(
        token,
        secret,
        write_confirmation=HIGH_SKU_WRITE_CONFIRMATION,
    )
    ui = UiContextReadClient(private_dir, canonical_contract_path)
    if client.token != ui.query_token or client.secret != ui.base_form.get("secret"):
        raise LiveImportError("canonical CREATE and UI-context session credentials differ")
    return client, ui, evidence


class HighSkuProbeRunner(ComplexLiveBatchRunner):
    def __init__(
        self,
        *args: Any,
        root: Path,
        capacity_audit_path: Path,
        **kwargs: Any,
    ) -> None:
        self.root = root
        self.capacity_audit_path = capacity_audit_path
        super().__init__(
            *args,
            expected_batch_size=1,
            expected_confirmation=HIGH_SKU_WRITE_CONFIRMATION,
            mode=HIGH_SKU_MODE,
            prohibited_product_numbers=HARD_PROHIBITED_PRODUCTS,
            **kwargs,
        )

    def _batch_preflight(self) -> None:
        if [item["product_number"] for item in self.items] != [HIGH_SKU_PRODUCT_NUMBER]:
            raise LiveImportError("only fixed 63-6602-492 is allowed in this probe")
        expected_hashes = self.selection.get("protected_frozen_evidence") or {}
        current_hashes = {
            "original_complex_batch_checkpoint_sha256": _file_sha256(
                self.root / "state/shijiu_complex_live_batch_checkpoint.json"
            ),
            "prior_bisection_selection_sha256": _file_sha256(
                self.root / "config/shijiu_complexity_bisection_batch.json"
            ),
            "prior_bisection_checkpoint_sha256": _file_sha256(
                self.root / "state/shijiu_complexity_bisection_checkpoint.json"
            ),
            "prior_bisection_report_sha256": _file_sha256(
                self.root / "deliverables/shijiu_import/complexity_bisection_report.json"
            ),
        }
        if current_hashes != expected_hashes:
            raise LiveImportError("protected frozen complex/bisection evidence changed")
        if _file_sha256(self.capacity_audit_path) != self.selection.get("capacity_audit_sha256"):
            raise LiveImportError("capacity audit changed after the single-product batch was frozen")
        audit = json.loads(self.capacity_audit_path.read_text(encoding="utf-8"))
        if (
            audit.get("status") != "COMPLETED_READ_ONLY"
            or audit.get("target_mutation_requests_sent") != 0
            or not (audit.get("scope") or {}).get(
                "deterministic_evenly_spaced_page_sample_completed"
            )
        ):
            raise LiveImportError("strict read-only target capacity audit gate failed")
        super()._batch_preflight()

    def _report_documents(self) -> tuple[dict[str, Any], dict[str, Any]]:
        report, readbacks = super()._report_documents()
        completed = report["status"] == "COMPLETED" and report["verified_product_count"] == 1
        report.update({
            "probe_product_number": HIGH_SKU_PRODUCT_NUMBER,
            "independent_of_prior_serial_gate": True,
            "prior_bisection_remains_frozen": True,
            "original_complex_five_remain_frozen": True,
            "maximum_product_create_requests": 1,
            "fourteen_sku_scale_verified": completed and report["verified_sku_count"] == 14,
            "all_color_size_pairs_verified_via_exact_specification": completed and all(
                sku.get("color_size_verified_via_exact_specification") is True
                for result in readbacks.get("results") or []
                for sku in result.get("skus") or []
            ),
            "staged_update_executed": False,
            "bulk_20_executed": False,
        })
        return report, readbacks


def build_high_sku_diagnosis(
    checkpoint: dict[str, Any],
    capacity_audit_sha256: str,
) -> dict[str, Any]:
    record = (checkpoint.get("records") or {}).get(HIGH_SKU_PRODUCT_NUMBER) or {}
    passed = (
        checkpoint.get("status") == "COMPLETED"
        and record.get("state") == "READBACK_VERIFIED"
        and record.get("mapping_persisted") is True
        and (record.get("readback") or {}).get("sku_count") == 14
    )
    if passed:
        decision = "SKU_SCALE_14_VERIFIED_RICH_MEDIA_SCALE_PRIMARY_REMAINING_FACTOR"
        explanation = (
            "The independent 14-SKU, six-carousel/four-detail probe persisted and every SKU "
            "strongly read back. SKU scale through 14 is therefore observed working; rich-media "
            "field scale is the primary remaining scale signal, not a proven hard limit."
        )
    else:
        decision = "DUAL_FACTOR_UNRESOLVED_SKU_AND_RICH_MEDIA_SCALE"
        explanation = (
            "The 14-SKU probe did not complete strong persistence verification. Both SKU scale and "
            "rich-media scale remain unresolved; image/detail size is not treated as the sole cause."
        )
    return {
        "schema_version": 1,
        "generated_at": now(),
        "decision": decision,
        "explanation": explanation,
        "product_number": HIGH_SKU_PRODUCT_NUMBER,
        "state": record.get("state"),
        "create_attempts": int(record.get("create_attempts") or 0),
        "uploaded_image_count": sum(
            row.get("status") == "UPLOADED"
            for row in (record.get("image_uploads") or {}).values()
        ),
        "verified_sku_count": (record.get("readback") or {}).get("sku_count", 0),
        "mapping_persisted": bool(record.get("mapping_persisted")),
        "fourteen_sku_scale_passed": passed,
        "capacity_audit_sha256": capacity_audit_sha256,
        "server_hard_limit_proven": False,
        "failed_13_9310_490_retried": False,
        "failed_00_4000_057_retried": False,
        "original_complex_five_continued": False,
        "prior_bisection_continued": False,
        "replacement_product_attempted": False,
        "staged_update_executed": False,
        "bulk_20_generated_or_executed": False,
        "legacy_reference_modified": False,
        "sensitive_values_included": False,
    }


def build_staged_rich_media_plan() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": now(),
        "status": "PLANNED_NOT_EXECUTED",
        "prerequisite": "14-SKU independent CREATE probe strongly verified",
        "scope": "future newly authorized MIKIHOUSE products only",
        "create_stage": {
            "fields": "core product + complete spec_name + complete sku_info + master_graph + controlled carousel subset",
            "controlled_carousel_url_count": 4,
            "good_detail_pics": "empty at CREATE",
            "good_details": "text-only minimal detail at CREATE",
            "required_readback": "exact good_name -> product_id -> getFormatInfo; verify every SKU and submitted image",
        },
        "update_stages": [
            {
                "sequence": 1,
                "purpose": "resubmit the full native edit payload while adding remaining carousel URLs in bounded chunks",
                "maximum_new_images_per_step": 8,
                "required_readback": "exact ordered broadcast equality after every step",
            },
            {
                "sequence": 2,
                "purpose": "resubmit the full native edit payload while adding good_detail_pics in bounded chunks",
                "maximum_new_images_per_step": 8,
                "required_readback": "exact ordered detail-picture inclusion after every step",
            },
            {
                "sequence": 3,
                "purpose": "install final HTML detail content only after image fields verify",
                "maximum_new_images_per_step": 8,
                "required_readback": "exact expected COS URLs and detail content hash after every step",
            },
        ],
        "fail_closed": "any mismatch freezes the product; no retry or next product",
        "update_contract_requirement": "must use the repository-audited Shijiu native edit path with separate explicit write authorization",
        "rollback_prerequisite": "persist the complete pre-update getFormatInfo snapshot before every stage",
        "execution_authorized": False,
        "update_requests_sent": 0,
        "legacy_cleanup_executed": False,
        "bulk_20_executed": False,
        "sensitive_values_included": False,
    }
