from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .catalog import calculate_mini_program_price_jpy
from .shijiu_canonical_create import load_verified_browser_credentials
from .shijiu_complex_import import (
    TARGET_CATEGORY_ID,
    UI_READ_INITIAL_BACKOFF_SECONDS,
    UI_READ_MAX_RETRIES,
    UI_TRANSIENT_HTTP_STATUS_CODES,
    UiContextReadClient,
    _metrics,
)
from .shijiu_import import (
    EXPECTED_SPECIAL_COUNT,
    SOURCE_CODE,
    content_sha256,
    load_mapping_state,
    map_product_to_shijiu,
    now,
    write_json_atomic,
)
from .shijiu_live_import import LiveImportError, ShijiuLiveClient
from .shijiu_production_architecture_verification import (
    FINAL_E2E_PROTECTED_FILES,
    build_final_e2e_conclusion,
    build_representative_next_20_plan,
)
from .shijiu_staged_media_complete import (
    CompleteStagedMediaRunner,
    _configured_product_numbers,
    _mapped_row_hashes,
    initialize_complete_checkpoint,
)
from .shijiu_staged_media_import import (
    StagedMediaRunner,
    _file_sha256,
    image_reference_sets,
    initial_checkpoint,
    stage_plan,
    validate_good_details_contract,
)
from .shijiu_writer_mutex import mutex_evidence_satisfied


RICHTEXT_E2E_MODE = "MIKIHOUSE_RICHTEXT_CONTRACT_FINAL_E2E_VALIDATION"
RICHTEXT_E2E_WRITE_CONFIRMATION = "MIKIHOUSE_RICHTEXT_CONTRACT_FINAL_E2E_SINGLE_STEP"
FROZEN_PRODUCT_NUMBER = "10-9332-796"
MINIMUM_DETAIL_PICS = 16
RICHTEXT_E2E_PROTECTED_FILES = tuple(dict.fromkeys((
    *FINAL_E2E_PROTECTED_FILES,
    "config/shijiu_richtext_contract.json",
    "deliverables/shijiu_import/richtext_contract_comparison.json",
    "deliverables/shijiu_import/richtext_contract_readiness.json",
    "deliverables/shijiu_import/richtext_contract_readonly_audit.json",
    "deliverables/shijiu_import/richtext_native_image_edit_capture.json",
    "deliverables/shijiu_import/richtext_native_text_edit_capture.json",
)))


def _product_from_master(master: dict[str, Any], number: str) -> dict[str, Any]:
    product = next(
        (row for row in master.get("products") or [] if row.get("product_number") == number),
        None,
    )
    if not product or not product.get("active"):
        raise LiveImportError("frozen rich-text E2E product is missing or inactive")
    return product


def build_richtext_e2e_selection(
    root: Path,
    master: dict[str, Any],
    special: set[str],
    mapping: dict[str, Any],
    category: dict[str, Any],
    frozen_readiness: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if len(special) != EXPECTED_SPECIAL_COUNT or FROZEN_PRODUCT_NUMBER in special:
        raise LiveImportError("permanent PDF special exclusion boundary failed")
    frozen = frozen_readiness.get("frozen_next_product") or {}
    if (
        frozen_readiness.get("status") != "READY_FOR_LAST_ONE_PRODUCT_MIKIHOUSE_E2E_VALIDATION"
        or frozen.get("product_number") != FROZEN_PRODUCT_NUMBER
        or frozen_readiness.get("pdf_special_exclusion_count") != EXPECTED_SPECIAL_COUNT
        or frozen_readiness.get("pdf_special_selected") is not False
    ):
        raise LiveImportError("rich-text readiness does not freeze the required product")
    mapping_row = (mapping.get("products") or {}).get(FROZEN_PRODUCT_NUMBER) or {}
    if mapping_row.get("shijiu_product_id") not in (None, ""):
        raise LiveImportError("frozen rich-text E2E product is already mapped")
    product = _product_from_master(master, FROZEN_PRODUCT_NUMBER)
    names = Counter(str(row.get("name") or "").strip() for row in master.get("products") or [])
    if names[str(product.get("name") or "").strip()] != 1:
        raise LiveImportError("frozen rich-text E2E product name is not source-unique")
    item = map_product_to_shijiu(product, category, excluded_product_numbers=special)
    if not item.get("publish_ready"):
        raise LiveImportError("frozen rich-text E2E product is not publishable")
    refs = image_reference_sets(item)
    stages = stage_plan(item)
    expected_stages = frozen_readiness.get("planned_stages") or []
    if (
        item["payload_sha256"] != frozen.get("source_payload_sha256")
        or stages != expected_stages
        or len(item.get("source_variants") or []) != 6
        or len(item.get("image_upload_plan") or []) != 18
        or len(refs["all_broadcast"]) != 18
        or len(refs["all_detail"]) != MINIMUM_DETAIL_PICS
        or [stage["key"] for stage in stages] != [
            "CREATE_CORE",
            "BROADCAST_5_12",
            "BROADCAST_13_18",
            "DETAIL_PICS_1_8",
            "DETAIL_PICS_9_16",
        ]
    ):
        raise LiveImportError("frozen rich-text source payload or five-stage plan drift")
    validate_good_details_contract(item["shijiu_payload_preview"].get("good_details"))
    historical = _configured_product_numbers(root)
    if FROZEN_PRODUCT_NUMBER in historical:
        # The new dedicated selection file is included for future exclusion only;
        # it must not turn its own already-frozen product into a historical retry.
        historical.remove(FROZEN_PRODUCT_NUMBER)
    metrics = _metrics(product)
    selection = {
        "schema_version": 1,
        "generated_at": now(),
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "mode": RICHTEXT_E2E_MODE,
        "selection_policy": "exactly the product frozen by richtext_contract_readiness.json",
        "fixed_target_category_id": TARGET_CATEGORY_ID,
        "pdf_special_exclusion_count": len(special),
        "historical_prohibited_product_numbers": sorted(historical),
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
            relative: _file_sha256(root / relative) for relative in RICHTEXT_E2E_PROTECTED_FILES
        },
        "protected_existing_mapping_row_hashes": _mapped_row_hashes(mapping),
        "product": {
            **copy.deepcopy(frozen),
            "variant_count": metrics["variant_count"],
            "available_variant_count": metrics["available_variant_count"],
            "color_count": metrics["color_count"],
            "size_count": metrics["size_count"],
            "broadcast_count": len(refs["all_broadcast"]),
            "detail_pic_count": len(refs["all_detail"]),
        },
        "stages": stages,
    }
    return item, selection


def load_richtext_e2e_candidate(
    master: dict[str, Any],
    special: set[str],
    category: dict[str, Any],
    selection: dict[str, Any],
) -> dict[str, Any]:
    product_meta = selection.get("product") or {}
    number = str(product_meta.get("product_number") or "")
    if (
        selection.get("mode") != RICHTEXT_E2E_MODE
        or selection.get("fixed_target_category_id") != TARGET_CATEGORY_ID
        or number != FROZEN_PRODUCT_NUMBER
        or number in special
        or number in set(selection.get("historical_prohibited_product_numbers") or [])
        or int(product_meta.get("variant_count") or 0) != 6
        or int(product_meta.get("broadcast_count") or 0) != 18
        or int(product_meta.get("detail_pic_count") or 0) != MINIMUM_DETAIL_PICS
    ):
        raise LiveImportError("rich-text E2E frozen selection boundary failed")
    product = _product_from_master(master, number)
    item = map_product_to_shijiu(product, category, excluded_product_numbers=special)
    if (
        item["payload_sha256"] != product_meta.get("source_payload_sha256")
        or stage_plan(item) != selection.get("stages")
    ):
        raise LiveImportError("rich-text E2E frozen source/stage payload drift")
    validate_good_details_contract(item["shijiu_payload_preview"].get("good_details"))
    return item


class RichtextContractE2ERunner(CompleteStagedMediaRunner):
    def __init__(
        self,
        client: ShijiuLiveClient,
        ui: UiContextReadClient,
        item: dict[str, Any],
        special: set[str],
        category: dict[str, Any],
        selection: dict[str, Any],
        *,
        root: Path,
        checkpoint_path: Path,
        mapping_path: Path,
        report_path: Path,
        readbacks_path: Path,
        confirmation: str,
        mode: str = RICHTEXT_E2E_MODE,
        expected_confirmation: str = RICHTEXT_E2E_WRITE_CONFIRMATION,
        protected_frozen_files: tuple[str, ...] = RICHTEXT_E2E_PROTECTED_FILES,
    ) -> None:
        if not checkpoint_path.exists():
            checkpoint = initial_checkpoint(item, selection, mode=mode)
            write_json_atomic(checkpoint_path, initialize_complete_checkpoint(checkpoint, item))
        self.complete_selection = selection
        self.richtext_protected_files = protected_frozen_files
        StagedMediaRunner.__init__(
            self,
            client,
            ui,
            item,
            special,
            category,
            selection,
            root=root,
            checkpoint_path=checkpoint_path,
            mapping_path=mapping_path,
            report_path=report_path,
            readbacks_path=readbacks_path,
            confirmation=confirmation,
            mode=mode,
            expected_confirmation=expected_confirmation,
            prohibited_product_numbers=set(selection["historical_prohibited_product_numbers"]),
            protected_frozen_files=protected_frozen_files,
        )

    def _assert_protected_complete_boundary(self) -> None:
        current_files = {
            relative: _file_sha256(self.root / relative)
            for relative in self.richtext_protected_files
        }
        if current_files != self.complete_selection.get("protected_frozen_evidence"):
            raise LiveImportError("historical frozen/rich-text contract evidence changed")
        if _file_sha256(self.root / "config/shijiu_richtext_contract.json") != self.complete_selection.get(
            "richtext_contract_sha256"
        ):
            raise LiveImportError("verified rich-text contract changed")
        mapping = load_mapping_state(self.mapping_path)
        protected = self.complete_selection.get("protected_existing_mapping_row_hashes") or {}
        current = {number: content_sha256(mapping["products"][number]) for number in protected}
        if current != protected:
            raise LiveImportError("previously verified mapping row changed")

    def _report(self) -> dict[str, Any]:
        report = StagedMediaRunner._report(self)
        resource = self.checkpoint.get("resource_preflight") or {}
        ledger = self.checkpoint.get("request_ledger") or []
        technical_complete = self.checkpoint.get("status") == "COMPLETED"
        mutex_verified = mutex_evidence_satisfied(self.checkpoint)
        report["technical_checkpoint_status"] = report.pop("status")
        report["status"] = (
            "VERIFIED"
            if technical_complete and mutex_verified
            else "TECHNICALLY_VERIFIED_MUTEX_EVIDENCE_NOT_CAPTURED"
            if technical_complete
            else report["technical_checkpoint_status"]
        )
        report.update({
            "product_number": self.item["product_number"],
            "complete_resource_preflight": {
                "status": resource.get("status"),
                "required_reference_count": resource.get("required_reference_count"),
                "verified_reference_count": resource.get("verified_reference_count", 0),
                "all_domains_approved_before_download": resource.get(
                    "all_domains_approved_before_download", False
                ),
                "shijiu_requests_sent": resource.get("shijiu_requests_sent", 0),
                "shijiu_write_requests_sent": resource.get("shijiu_write_requests_sent", 0),
            },
            "good_details_contract": "TEXT_OR_LIGHT_HTML_NO_IMAGE_OR_URL_MAX_1024",
            "detail_images_carried_by": "good_detail_pics",
            "ui_read_retry_policy": self.complete_selection["ui_read_retry_policy"],
            "ui_read_retry_attempt_count": sum(
                int(row.get("retry_index") or 0) > 0 for row in ledger
            ),
            "mutation_auto_retry_count": 0,
            "technical_five_stage_readback_completed": technical_complete,
            "production_write_mutex_evidence_verified": mutex_verified,
            "production_architecture_verified": technical_complete and mutex_verified,
        })
        return report


def make_richtext_e2e_clients(
    private_dir: Path,
    canonical_contract_path: Path,
    *,
    write_confirmation: str = RICHTEXT_E2E_WRITE_CONFIRMATION,
) -> tuple[ShijiuLiveClient, UiContextReadClient, dict[str, Any]]:
    token, secret, evidence = load_verified_browser_credentials(private_dir, canonical_contract_path)
    client = ShijiuLiveClient(
        token,
        secret,
        write_confirmation=write_confirmation,
    )
    ui = UiContextReadClient(private_dir, canonical_contract_path)
    if client.token != ui.query_token or client.secret != ui.base_form.get("secret"):
        raise LiveImportError("canonical save and UI-context session credentials differ")
    return client, ui, evidence


def verify_live_source_exact(item: dict[str, Any], live: Any) -> dict[str, Any]:
    expected = {row["source_variant_sku"]: row for row in item["source_variants"]}
    actual = {row.sku: row for row in live.variants}
    if set(actual) != set(expected):
        raise LiveImportError("live source SKU set drift for frozen rich-text E2E product")
    variants: list[dict[str, Any]] = []
    for sku, source in expected.items():
        observed = actual[sku]
        selected_options = [
            {"name": row.name, "value": row.value} for row in observed.selected_options
        ]
        expected_options = list(source.get("selected_options") or [])
        mini_price = calculate_mini_program_price_jpy(observed.tax_included_price_jpy)
        if (
            observed.tax_included_price_jpy != source["tax_included_price_jpy"]
            or mini_price != source["mini_program_price_jpy"]
            or observed.in_stock != source["available_for_sale"]
            or observed.color != source["color"]
            or observed.size != source["size"]
            or observed.image_url != source["image_url"]
            or selected_options != expected_options
        ):
            raise LiveImportError(f"live source variant drift: {sku}")
        variants.append({
            "backend_sku_code": f"MIKI-{sku}",
            "tax_included_price_jpy": observed.tax_included_price_jpy,
            "mini_program_price_jpy": mini_price,
            "available_for_sale": observed.in_stock,
            "color": observed.color,
            "size": observed.size,
            "selected_options": selected_options,
            "variant_image_url_sha256": content_sha256(observed.image_url),
        })
    return {
        "verified_at": now(),
        "product_number": live.product_number,
        "product_name": live.name,
        "variant_count": len(actual),
        "variants": variants,
        "all_skus_prices_65pct_prices_stocks_options_and_images_match_master": True,
        "sensitive_values_included": False,
    }


def build_richtext_e2e_conclusion(checkpoint: dict[str, Any]) -> dict[str, Any]:
    result = build_final_e2e_conclusion(checkpoint)
    technical_verified = bool(result["production_import_architecture_verified"])
    mutex_verified = mutex_evidence_satisfied(checkpoint)
    result["technical_five_stage_readback_completed"] = technical_verified
    result["production_write_mutex_evidence_verified"] = mutex_verified
    result["production_import_architecture_verified"] = technical_verified and mutex_verified
    result["status"] = (
        "VERIFIED"
        if technical_verified and mutex_verified
        else "TECHNICALLY_VERIFIED_MUTEX_EVIDENCE_NOT_CAPTURED"
        if technical_verified
        else result.get("status")
    )
    result["production_architecture"] = (
        "LIGHTWEIGHT_CREATE_PLUS_STAGED_NATIVE_FULL_PAYLOAD_BROADCAST_AND_"
        "GOOD_DETAIL_PICS_UPDATE_PLUS_LIGHTWEIGHT_GOOD_DETAILS"
    )
    result["image_type_good_details_generated_or_attempted"] = False
    result["fail_closed_no_further_write"] = not mutex_verified
    if technical_verified and not mutex_verified:
        result["interpretation"] = (
            "All five payload/readback stages passed technically, but the production write window "
            "did not capture proof that no other Shijiu writer was active. AGENTS.md section 14 "
            "therefore forbids READY/COMPLETED and all further writes fail closed."
        )
    return result


__all__ = [
    "FROZEN_PRODUCT_NUMBER",
    "RICHTEXT_E2E_MODE",
    "RICHTEXT_E2E_WRITE_CONFIRMATION",
    "RichtextContractE2ERunner",
    "build_representative_next_20_plan",
    "build_richtext_e2e_conclusion",
    "build_richtext_e2e_selection",
    "load_richtext_e2e_candidate",
    "make_richtext_e2e_clients",
    "verify_live_source_exact",
]
