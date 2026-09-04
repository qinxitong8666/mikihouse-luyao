from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .shijiu_canonical_create import load_verified_browser_credentials
from .shijiu_complex_import import (
    TARGET_CATEGORY_ID,
    UI_READ_INITIAL_BACKOFF_SECONDS,
    UI_READ_MAX_RETRIES,
    UI_TRANSIENT_HTTP_STATUS_CODES,
    UiContextReadClient,
    _normalize_ui_detail,
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
from .shijiu_live_import import (
    ContractMismatchError,
    LiveImportError,
    ShijiuLiveClient,
    validate_product_readback,
)
from .shijiu_staged_media_complete import (
    COMPLETE_PROTECTED_FILES,
    CompleteStagedMediaRunner,
    _configured_product_numbers,
    build_next_20_frozen_plan,
    initialize_complete_checkpoint,
)
from .shijiu_staged_media_import import (
    StagedMediaRunner,
    _file_sha256,
    build_stage_payload,
    image_reference_sets,
    initial_checkpoint,
    stage_plan,
)


DETAIL_HTML_MODE = "STAGED_DETAIL_PICS_AND_FINAL_HTML_SINGLE_REAL_VALIDATION"
DETAIL_HTML_WRITE_CONFIRMATION = "MIKIHOUSE_STAGED_DETAIL_HTML_SINGLE_STEP"
PROTECTED_COMPLETE_PRODUCT = "10-9129-792"
PROTECTED_COMPLETE_PRODUCT_ID = "9358255"
DETAIL_HTML_PROTECTED_FILES = tuple(dict.fromkeys((
    *COMPLETE_PROTECTED_FILES,
    "config/shijiu_staged_rich_media_complete_single.json",
    "state/shijiu_staged_rich_media_complete_single_checkpoint.json",
    "deliverables/shijiu_import/staged_rich_media_complete_candidate.json",
    "deliverables/shijiu_import/staged_rich_media_complete_capacity_conclusion.json",
    "deliverables/shijiu_import/staged_rich_media_complete_readbacks.json",
    "deliverables/shijiu_import/staged_rich_media_complete_readiness.json",
    "deliverables/shijiu_import/staged_rich_media_complete_resource_preflight.json",
    "deliverables/shijiu_import/staged_rich_media_complete_validation_report.json",
)))


def _mapped_row_hashes(mapping: dict[str, Any]) -> dict[str, str]:
    return {
        number: content_sha256(row)
        for number, row in sorted(mapping.get("products", {}).items())
        if row.get("shijiu_product_id")
    }


def select_detail_html_candidate(
    root: Path,
    master: dict[str, Any],
    special: set[str],
    mapping: dict[str, Any],
    category: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if len(special) != EXPECTED_SPECIAL_COUNT:
        raise LiveImportError("permanent PDF special exclusion count changed")
    prohibited = _configured_product_numbers(root)
    names = Counter(str(row.get("name") or "").strip() for row in master.get("products") or [])
    eligible: list[tuple[tuple[Any, ...], dict[str, Any], dict[str, Any], Counter[str]]] = []
    for product in master.get("products") or []:
        number = str(product.get("product_number") or "")
        variants = list(product.get("variants") or [])
        roles: Counter[str] = Counter(
            str(row.get("role") or "") for row in product.get("ordered_images") or []
        )
        if (
            not number
            or number in special
            or number in prohibited
            or not product.get("active")
            or (mapping.get("products", {}).get(number) or {}).get("shijiu_product_id") not in (None, "")
            or names[str(product.get("name") or "").strip()] != 1
            or not 2 <= len(variants) <= 6
            or not roles["product_gallery"]
            or not roles["detail"]
        ):
            continue
        item = map_product_to_shijiu(product, category, excluded_product_numbers=special)
        if not item.get("publish_ready"):
            continue
        refs = image_reference_sets(item)
        broadcast_count = len(refs["all_broadcast"])
        detail_count = len(refs["all_detail"])
        full_html = str(item["shijiu_payload_preview"].get("good_details") or "")
        if (
            not 12 <= broadcast_count <= 20
            or detail_count < 8
            or "{{SHIJIU_COS_URL:" not in full_html
        ):
            continue
        rank = (
            0 if 12 <= detail_count <= 20 else 1,
            abs(detail_count - 16),
            abs(broadcast_count - 16),
            abs(len(variants) - 4),
            number,
        )
        eligible.append((rank, item, _metrics(product), roles))
    if not eligible:
        raise LiveImportError("no detail/HTML candidate satisfies the frozen selection policy")
    _, item, metrics, roles = min(eligible, key=lambda row: row[0])
    refs = image_reference_sets(item)
    selection = {
        "schema_version": 1,
        "generated_at": now(),
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "mode": DETAIL_HTML_MODE,
        "selection_policy": (
            "deterministic: active, publishable, unmapped, non-special, never attempted, "
            "source-unique name, 2-6 variants, explicit gallery+detail roles, 12-20 "
            "broadcast URLs, at least 8 detail-pic URLs, image-bearing final HTML; then "
            "prefer 12-20 details and closest to 16 details/16 broadcast/4 variants"
        ),
        "fixed_target_category_id": TARGET_CATEGORY_ID,
        "pdf_special_exclusion_count": len(special),
        "candidate_pool_count": len(eligible),
        "historical_prohibited_product_numbers": sorted(prohibited),
        "minimum_required_verified_detail_pic_count": 8,
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
        "protected_frozen_evidence": {
            relative: _file_sha256(root / relative) for relative in DETAIL_HTML_PROTECTED_FILES
        },
        "protected_existing_mapping_row_hashes": _mapped_row_hashes(mapping),
        "required_protected_mapping_ids": {
            PROTECTED_COMPLETE_PRODUCT: PROTECTED_COMPLETE_PRODUCT_ID,
        },
        "product": {
            "product_number": item["product_number"],
            "good_name": item["shijiu_payload_preview"]["good_name"],
            "name_unique_in_source": True,
            "variant_count": metrics["variant_count"],
            "available_variant_count": metrics["available_variant_count"],
            "color_count": metrics["color_count"],
            "size_count": metrics["size_count"],
            "official_image_count": metrics["image_count"],
            "role_counts": dict(sorted(roles.items())),
            "broadcast_count": len(refs["all_broadcast"]),
            "detail_pic_count": len(refs["all_detail"]),
            "full_good_details_contains_images": True,
            "source_payload_sha256": item["payload_sha256"],
        },
        "stages": stage_plan(item),
    }
    return item, selection


def load_detail_html_candidate(
    master: dict[str, Any],
    special: set[str],
    category: dict[str, Any],
    selection: dict[str, Any],
) -> dict[str, Any]:
    number = str((selection.get("product") or {}).get("product_number") or "")
    if (
        selection.get("mode") != DETAIL_HTML_MODE
        or selection.get("fixed_target_category_id") != TARGET_CATEGORY_ID
        or number in special
        or number in set(selection.get("historical_prohibited_product_numbers") or [])
    ):
        raise LiveImportError("detail/HTML frozen selection boundary failed")
    product = next(
        (row for row in master.get("products") or [] if row.get("product_number") == number),
        None,
    )
    if not product or not product.get("active"):
        raise LiveImportError("detail/HTML source product is missing or inactive")
    item = map_product_to_shijiu(product, category, excluded_product_numbers=special)
    if (
        item["payload_sha256"] != selection["product"]["source_payload_sha256"]
        or stage_plan(item) != selection.get("stages")
    ):
        raise LiveImportError("detail/HTML frozen source/stage payload drift")
    return item


class DetailHtmlStagedRunner(CompleteStagedMediaRunner):
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
    ) -> None:
        if not checkpoint_path.exists():
            checkpoint = initial_checkpoint(item, selection, mode=DETAIL_HTML_MODE)
            write_json_atomic(checkpoint_path, initialize_complete_checkpoint(checkpoint, item))
        self.complete_selection = selection
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
            mode=DETAIL_HTML_MODE,
            expected_confirmation=DETAIL_HTML_WRITE_CONFIRMATION,
            prohibited_product_numbers=set(selection["historical_prohibited_product_numbers"]),
            protected_frozen_files=DETAIL_HTML_PROTECTED_FILES,
        )

    def _assert_protected_complete_boundary(self) -> None:
        current_files = {
            relative: _file_sha256(self.root / relative)
            for relative in DETAIL_HTML_PROTECTED_FILES
        }
        if current_files != self.complete_selection.get("protected_frozen_evidence"):
            raise LiveImportError("historical frozen evidence changed during detail/HTML validation")
        mapping = load_mapping_state(self.mapping_path)
        protected_hashes = self.complete_selection.get("protected_existing_mapping_row_hashes") or {}
        current_rows = {
            number: content_sha256(mapping["products"][number]) for number in protected_hashes
        }
        if current_rows != protected_hashes:
            raise LiveImportError("previously verified mapping row changed")
        for number, product_id in (
            self.complete_selection.get("required_protected_mapping_ids") or {}
        ).items():
            if str(mapping["products"][number].get("shijiu_product_id")) != str(product_id):
                raise LiveImportError(f"protected mapping identity changed: {number}")

    def _report(self) -> dict[str, Any]:
        report = StagedMediaRunner._report(self)
        resource = self.checkpoint.get("resource_preflight") or {}
        ledger = self.checkpoint.get("request_ledger") or []
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
            "ui_read_retry_policy": self.complete_selection["ui_read_retry_policy"],
            "ui_read_retry_attempt_count": sum(
                int(row.get("retry_index") or 0) > 0 for row in ledger
            ),
            "ui_transient_read_error_count": sum(
                row.get("outcome") == "TRANSIENT_READ_ERROR" for row in ledger
            ),
            "mutation_auto_retry_count": 0,
            "protected_complete_product_preserved": True,
            "protected_complete_product_id": PROTECTED_COMPLETE_PRODUCT_ID,
            "production_architecture_verified": self.checkpoint.get("status") == "COMPLETED",
        })
        return report


def make_detail_html_clients(
    private_dir: Path, canonical_contract_path: Path
) -> tuple[ShijiuLiveClient, UiContextReadClient, dict[str, Any]]:
    token, secret, evidence = load_verified_browser_credentials(private_dir, canonical_contract_path)
    client = ShijiuLiveClient(
        token,
        secret,
        write_confirmation=DETAIL_HTML_WRITE_CONFIRMATION,
    )
    ui = UiContextReadClient(private_dir, canonical_contract_path)
    if client.token != ui.query_token or client.secret != ui.base_form.get("secret"):
        raise LiveImportError("canonical save and UI-context session credentials differ")
    return client, ui, evidence


def analyze_frozen_detail_readback(
    item: dict[str, Any], checkpoint: dict[str, Any]
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": now(),
        "status": "NOT_APPLICABLE",
        "product_number": item["product_number"],
        "checkpoint_remains_frozen": checkpoint.get("status") == "FROZEN_ON_FIRST_ANOMALY",
        "target_requests_sent_by_analysis": 0,
        "target_mutations_sent_by_analysis": 0,
        "sensitive_values_included": False,
    }
    if checkpoint.get("status") != "FROZEN_ON_FIRST_ANOMALY":
        return result
    cursor = int(checkpoint.get("stage_cursor") or 0)
    stages = checkpoint.get("stages") or []
    if cursor >= len(stages):
        return result
    stage = stages[cursor]
    confirmation = stage.get("post_failure_readonly_confirmation") or {}
    snapshots = confirmation.get("snapshots") or []
    if (
        stage.get("operation") != "UPDATE_DETAIL_PICS"
        or not checkpoint.get("first_failed_state", {}).get("mutation_request_sent")
        or len(snapshots) != 1
    ):
        return result
    payload = build_stage_payload(
        item,
        stage,
        checkpoint.get("image_uploads") or {},
        product_id=str(checkpoint["shijiu_product_id"]),
    )
    business_payload = {key: value for key, value in payload.items() if key != "id"}
    snapshot = snapshots[0]["getFormatInfo"]
    data = snapshot.get("data") if isinstance(snapshot.get("data"), dict) else {}
    try:
        readback = validate_product_readback(
            item,
            business_payload,
            str(checkpoint["shijiu_product_id"]),
            _normalize_ui_detail(snapshot),
            list_row={
                "id": checkpoint["shijiu_product_id"],
                "state": data.get("state"),
                "is_shelf": data.get("is_shelf"),
            },
            require_is_shelf=False,
            require_exact_good_details=True,
        )
    except ContractMismatchError as error:
        result.update({
            "status": "POST_MUTATION_SNAPSHOT_STILL_FAILS_CORRECTED_STAGE_CONTRACT",
            "error": {"type": type(error).__name__, "message": str(error)},
        })
        return result
    result.update({
        "status": "POST_MUTATION_SNAPSHOT_PASSES_CORRECTED_STAGE_CONTRACT",
        "root_cause": (
            "historical validator incorrectly required every good_detail_pics URL to already "
            "appear in the intentionally minimal pre-final good_details HTML"
        ),
        "original_freeze_preserved": True,
        "original_mutation_was_not_retried": True,
        "observed_shijiu_product_id": readback["shijiu_product_id"],
        "observed_broadcast_count": len(readback["carousel_urls"]),
        "observed_good_detail_pics_count": len(readback["detail_image_urls"]),
        "observed_sku_count": readback["sku_count"],
        "observed_good_details_sha256": readback["good_details_sha256"],
        "all_skus_prices_stocks_specs_and_images_verified": True,
        "category_verified": readback["target_category_id"] == TARGET_CATEGORY_ID,
        "readback_snapshot_sha256": content_sha256(snapshot),
    })
    return result


def build_detail_html_conclusion(
    checkpoint: dict[str, Any], forensic: dict[str, Any] | None = None
) -> dict[str, Any]:
    verified = [row for row in checkpoint.get("stages") or [] if row.get("state") == "VERIFIED"]
    max_broadcast = max(
        (int((row.get("metrics") or {}).get("broadcast_url_count") or 0) for row in verified),
        default=0,
    )
    max_details = max(
        (int((row.get("metrics") or {}).get("good_detail_pics_url_count") or 0) for row in verified),
        default=0,
    )
    html_verified = bool(verified and verified[-1].get("operation") == "UPDATE_GOOD_DETAILS")
    architecture_verified = (
        checkpoint.get("status") == "COMPLETED" and max_details >= 8 and html_verified
    )
    ledger = checkpoint.get("request_ledger") or []
    forensic = forensic or {}
    return {
        "schema_version": 1,
        "generated_at": now(),
        "status": checkpoint.get("status"),
        "product_number": checkpoint.get("product_number"),
        "shijiu_product_id": checkpoint.get("shijiu_product_id"),
        "resource_preflight_status": (checkpoint.get("resource_preflight") or {}).get("status"),
        "verified_stage_keys": [row["key"] for row in verified],
        "maximum_verified_broadcast_url_count": max_broadcast,
        "maximum_verified_good_detail_pics_url_count": max_details,
        "minimum_required_good_detail_pics_satisfied": max_details >= 8,
        "maximum_forensically_verified_good_detail_pics_url_count": forensic.get(
            "observed_good_detail_pics_count", 0
        ),
        "minimum_required_good_detail_pics_forensically_verified": (
            forensic.get("status")
            == "POST_MUTATION_SNAPSHOT_PASSES_CORRECTED_STAGE_CONTRACT"
            and int(forensic.get("observed_good_detail_pics_count") or 0) >= 8
        ),
        "final_good_details_html_verified": html_verified,
        "production_import_architecture_verified": architecture_verified,
        "post_failure_forensic_readback": {
            "status": forensic.get("status", "NOT_CAPTURED"),
            "observed_good_detail_pics_count": forensic.get(
                "observed_good_detail_pics_count"
            ),
            "all_skus_prices_stocks_specs_and_images_verified": forensic.get(
                "all_skus_prices_stocks_specs_and_images_verified"
            ),
            "checkpoint_remains_frozen": forensic.get("checkpoint_remains_frozen"),
        },
        "first_failed_or_blocked_state": checkpoint.get("first_failed_state"),
        "ui_read_retry_attempt_count": sum(
            int(row.get("retry_index") or 0) > 0 for row in ledger
        ),
        "ui_transient_read_error_count": sum(
            row.get("outcome") == "TRANSIENT_READ_ERROR" for row in ledger
        ),
        "mutation_auto_retry_count": 0,
        "interpretation": (
            "Lightweight CREATE plus staged native full-payload broadcast, ordered detail-picture, "
            "and final image-bearing HTML updates all passed strong UI-context readback."
            if architecture_verified else
            "The first 8-detail-pic UPDATE returned success and its saved post-failure snapshot "
            "passes the corrected staged contract, but the original false-negative freeze is "
            "preserved. The 16-detail and final-HTML stages were not executed, so production "
            "readiness remains false."
            if forensic.get("status") == "POST_MUTATION_SNAPSHOT_PASSES_CORRECTED_STAGE_CONTRACT"
            else
            "The single product stopped before the complete detail-picture and final-HTML chain "
            "was strongly verified; production readiness remains false."
        ),
        "server_hard_limit_proven": False,
        "sensitive_values_included": False,
    }
