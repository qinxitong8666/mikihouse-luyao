from __future__ import annotations

import hashlib
import json
import re
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
from .shijiu_staged_detail_html import DETAIL_HTML_PROTECTED_FILES, _mapped_row_hashes
from .shijiu_staged_media_complete import (
    CompleteStagedMediaRunner,
    _configured_product_numbers,
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


FINAL_E2E_MODE = "MIKIHOUSE_PRODUCTION_ARCHITECTURE_FINAL_E2E_VALIDATION"
FINAL_E2E_WRITE_CONFIRMATION = "MIKIHOUSE_PRODUCTION_ARCHITECTURE_FINAL_E2E_SINGLE_STEP"
MINIMUM_DETAIL_PICS = 16
FINAL_E2E_PROTECTED_FILES = tuple(dict.fromkeys((
    *DETAIL_HTML_PROTECTED_FILES,
    "config/shijiu_staged_detail_html_single.json",
    "state/shijiu_staged_detail_html_single_checkpoint.json",
    "deliverables/shijiu_import/staged_detail_html_candidate.json",
    "deliverables/shijiu_import/staged_detail_html_capacity_conclusion.json",
    "deliverables/shijiu_import/staged_detail_html_false_negative_forensics.json",
    "deliverables/shijiu_import/staged_detail_html_readbacks.json",
    "deliverables/shijiu_import/staged_detail_html_readiness.json",
    "deliverables/shijiu_import/staged_detail_html_resource_preflight.json",
    "deliverables/shijiu_import/staged_detail_html_validation_report.json",
)))


def _historical_product_numbers(root: Path) -> set[str]:
    numbers = _configured_product_numbers(root)
    detail = root / "config/shijiu_staged_detail_html_single.json"
    if detail.exists():
        product = json.loads(detail.read_text(encoding="utf-8")).get("product") or {}
        if product.get("product_number"):
            numbers.add(str(product["product_number"]))
    return numbers


def select_final_e2e_candidate(
    root: Path,
    master: dict[str, Any],
    special: set[str],
    mapping: dict[str, Any],
    category: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if len(special) != EXPECTED_SPECIAL_COUNT:
        raise LiveImportError("permanent PDF special exclusion count changed")
    prohibited = _historical_product_numbers(root)
    names = Counter(str(row.get("name") or "").strip() for row in master.get("products") or [])
    eligible: list[tuple[tuple[Any, ...], dict[str, Any], dict[str, Any], Counter[str]]] = []
    for product in master.get("products") or []:
        number = str(product.get("product_number") or "")
        variants = list(product.get("variants") or [])
        roles = Counter(str(row.get("role") or "") for row in product.get("ordered_images") or [])
        if (
            not number
            or number in special
            or number in prohibited
            or not product.get("active")
            or (mapping.get("products", {}).get(number) or {}).get("shijiu_product_id") not in (None, "")
            or names[str(product.get("name") or "").strip()] != 1
            or not 2 <= len(variants) <= 8
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
        light_details = str(item["shijiu_payload_preview"].get("good_details") or "")
        if (
            not 12 <= broadcast_count <= 20
            or not MINIMUM_DETAIL_PICS <= detail_count <= 20
            or not light_details
            or "<img" in light_details.lower()
            or "http://" in light_details.lower()
            or "https://" in light_details.lower()
            or len(light_details) > 1024
        ):
            continue
        metrics = _metrics(product)
        rank = (
            abs(detail_count - MINIMUM_DETAIL_PICS),
            abs(broadcast_count - 16),
            abs(len(variants) - 4),
            number,
        )
        eligible.append((rank, item, metrics, roles))
    if not eligible:
        raise LiveImportError("no final E2E candidate satisfies the frozen selection policy")
    _, item, metrics, roles = min(eligible, key=lambda row: row[0])
    refs = image_reference_sets(item)
    selection = {
        "schema_version": 1,
        "generated_at": now(),
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "mode": FINAL_E2E_MODE,
        "selection_policy": (
            "deterministic: active, publishable, unmapped, non-special, absent from every prior "
            "attempt/freeze, source-unique name, 2-8 variants, explicit gallery+detail roles, "
            "12-20 broadcast URLs, 16-20 detail-pic URLs, target-supported text/light HTML; then closest "
            "to 16 details/16 broadcast/4 variants and product_number"
        ),
        "fixed_target_category_id": TARGET_CATEGORY_ID,
        "pdf_special_exclusion_count": len(special),
        "candidate_pool_count": len(eligible),
        "historical_prohibited_product_numbers": sorted(prohibited),
        "minimum_required_verified_detail_pic_count": MINIMUM_DETAIL_PICS,
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
            relative: _file_sha256(root / relative) for relative in FINAL_E2E_PROTECTED_FILES
        },
        "protected_existing_mapping_row_hashes": _mapped_row_hashes(mapping),
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
            "good_details_contract": "TEXT_OR_LIGHT_HTML_NO_IMAGE_OR_URL_MAX_1024",
            "source_payload_sha256": item["payload_sha256"],
        },
        "stages": stage_plan(item),
    }
    return item, selection


def load_final_e2e_candidate(
    master: dict[str, Any],
    special: set[str],
    category: dict[str, Any],
    selection: dict[str, Any],
) -> dict[str, Any]:
    number = str((selection.get("product") or {}).get("product_number") or "")
    if (
        selection.get("mode") != FINAL_E2E_MODE
        or selection.get("fixed_target_category_id") != TARGET_CATEGORY_ID
        or number in special
        or number in set(selection.get("historical_prohibited_product_numbers") or [])
        or int((selection.get("product") or {}).get("detail_pic_count") or 0) < MINIMUM_DETAIL_PICS
    ):
        raise LiveImportError("final E2E frozen selection boundary failed")
    product = next(
        (row for row in master.get("products") or [] if row.get("product_number") == number), None
    )
    if not product or not product.get("active"):
        raise LiveImportError("final E2E source product is missing or inactive")
    item = map_product_to_shijiu(product, category, excluded_product_numbers=special)
    if (
        item["payload_sha256"] != selection["product"]["source_payload_sha256"]
        or stage_plan(item) != selection.get("stages")
    ):
        raise LiveImportError("final E2E frozen source/stage payload drift")
    return item


class FinalE2EStagedRunner(CompleteStagedMediaRunner):
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
            checkpoint = initial_checkpoint(item, selection, mode=FINAL_E2E_MODE)
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
            mode=FINAL_E2E_MODE,
            expected_confirmation=FINAL_E2E_WRITE_CONFIRMATION,
            prohibited_product_numbers=set(selection["historical_prohibited_product_numbers"]),
            protected_frozen_files=FINAL_E2E_PROTECTED_FILES,
        )

    def _assert_protected_complete_boundary(self) -> None:
        current_files = {
            relative: _file_sha256(self.root / relative)
            for relative in FINAL_E2E_PROTECTED_FILES
        }
        if current_files != self.complete_selection.get("protected_frozen_evidence"):
            raise LiveImportError("historical frozen evidence changed during final E2E validation")
        mapping = load_mapping_state(self.mapping_path)
        protected = self.complete_selection.get("protected_existing_mapping_row_hashes") or {}
        current = {number: content_sha256(mapping["products"][number]) for number in protected}
        if current != protected:
            raise LiveImportError("previously verified mapping row changed")

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
            "ui_read_retry_attempt_count": sum(int(row.get("retry_index") or 0) > 0 for row in ledger),
            "ui_transient_read_error_count": sum(
                row.get("outcome") == "TRANSIENT_READ_ERROR" for row in ledger
            ),
            "mutation_auto_retry_count": 0,
            "production_architecture_verified": self.checkpoint.get("status") == "COMPLETED",
        })
        return report


def make_final_e2e_clients(
    private_dir: Path, canonical_contract_path: Path
) -> tuple[ShijiuLiveClient, UiContextReadClient, dict[str, Any]]:
    token, secret, evidence = load_verified_browser_credentials(private_dir, canonical_contract_path)
    client = ShijiuLiveClient(token, secret, write_confirmation=FINAL_E2E_WRITE_CONFIRMATION)
    ui = UiContextReadClient(private_dir, canonical_contract_path)
    if client.token != ui.query_token or client.secret != ui.base_form.get("secret"):
        raise LiveImportError("canonical save and UI-context session credentials differ")
    return client, ui, evidence


def build_final_e2e_conclusion(
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
    light_details_verified = bool(
        verified
        and all(
            int((row.get("metrics") or {}).get("good_details_characters") or 0) <= 1024
            and int((row.get("metrics") or {}).get("good_details_image_count") or 0) == 0
            and int((row.get("metrics") or {}).get("good_details_url_count") or 0) == 0
            for row in verified
        )
    )
    architecture_verified = (
        checkpoint.get("status") == "COMPLETED"
        and max_details >= MINIMUM_DETAIL_PICS
        and light_details_verified
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
        "minimum_required_good_detail_pics_count": MINIMUM_DETAIL_PICS,
        "minimum_required_good_detail_pics_satisfied": max_details >= MINIMUM_DETAIL_PICS,
        "final_good_details_html_verified": False,
        "good_details_text_or_light_html_verified": light_details_verified,
        "detail_images_carried_by": "good_detail_pics",
        "production_import_architecture_verified": architecture_verified,
        "first_failed_or_blocked_state": checkpoint.get("first_failed_state"),
        "post_failure_forensic_readback": {
            "status": forensic.get("status", "NOT_CAPTURED"),
            "observed_good_detail_pics_count": forensic.get("observed_good_detail_pics_count"),
            "observed_broadcast_count": forensic.get("observed_broadcast_count"),
            "all_non_html_fields_match_last_verified_state": forensic.get(
                "all_non_html_fields_match_last_verified_state"
            ),
            "mutation_was_not_retried": forensic.get("mutation_was_not_retried"),
        },
        "ui_read_retry_attempt_count": sum(int(row.get("retry_index") or 0) > 0 for row in ledger),
        "ui_transient_read_error_count": sum(
            row.get("outcome") == "TRANSIENT_READ_ERROR" for row in ledger
        ),
        "mutation_auto_retry_count": 0,
        "interpretation": (
            "Lightweight CREATE plus staged native full-payload broadcast, at least 16 ordered "
            "detail pictures in good_detail_pics, and target-supported text/light good_details "
            "all passed strong UI-context "
            "readback; the MIKIHOUSE production import architecture is VERIFIED."
            if architecture_verified else
            "The final-HTML save was acknowledged but the target retained the prior minimal HTML; "
            "the checkpoint is frozen, production architecture verification remains incomplete, "
            "and no replacement product may be attempted."
            if forensic.get("status") == "FINAL_HTML_MUTATION_ACKNOWLEDGED_BUT_TARGET_RETAINED_PRIOR_MINIMAL_HTML"
            else "The one-product final E2E chain stopped at its first anomaly; production architecture "
            "verification remains incomplete and no replacement product may be attempted."
        ),
        "sensitive_values_included": False,
    }


def analyze_frozen_final_html(
    item: dict[str, Any], checkpoint: dict[str, Any]
) -> dict[str, Any]:
    """Explain a frozen final-HTML save from already persisted evidence only."""
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
    if cursor < 1 or cursor >= len(stages):
        return result
    failed = stages[cursor]
    snapshots = (failed.get("post_failure_readonly_confirmation") or {}).get("snapshots") or []
    if (
        failed.get("operation") != "UPDATE_GOOD_DETAILS"
        or not (checkpoint.get("first_failed_state") or {}).get("mutation_request_sent")
        or len(snapshots) != 1
    ):
        return result
    product_id = str(checkpoint["shijiu_product_id"])
    uploads = checkpoint.get("image_uploads") or {}
    previous_payload = build_stage_payload(item, stages[cursor - 1], uploads, product_id=product_id)
    final_payload = build_stage_payload(item, failed, uploads, product_id=product_id)
    snapshot = snapshots[0]["getFormatInfo"]
    data = snapshot.get("data") if isinstance(snapshot.get("data"), dict) else {}
    try:
        readback = validate_product_readback(
            item,
            {key: value for key, value in previous_payload.items() if key != "id"},
            product_id,
            _normalize_ui_detail(snapshot),
            list_row={"id": product_id, "state": data.get("state"), "is_shelf": data.get("is_shelf")},
            require_is_shelf=False,
            require_exact_good_details=True,
        )
    except ContractMismatchError as error:
        result.update({
            "status": "POST_FAILURE_SNAPSHOT_REGRESSED_BEYOND_FINAL_HTML",
            "error": {"type": type(error).__name__, "message": str(error)},
        })
        return result
    expected_html = str(final_payload.get("good_details") or "")
    observed_html = str(data.get("good_details") or "")
    url_pattern = re.compile(r"https?://[^\s,\"'<>]+")
    expected_urls = list(dict.fromkeys(value.rstrip("\\") for value in url_pattern.findall(expected_html)))
    observed_urls = list(dict.fromkeys(value.rstrip("\\") for value in url_pattern.findall(observed_html)))
    matching_write_count = sum(
        row.get("semantic_operation") == "write"
        and row.get("path") == "/shopapi/Goods/newAddGood"
        and row.get("payload_sha256") == failed.get("payload_sha256")
        for row in checkpoint.get("request_ledger") or []
    )
    result.update({
        "status": "FINAL_HTML_MUTATION_ACKNOWLEDGED_BUT_TARGET_RETAINED_PRIOR_MINIMAL_HTML",
        "observed_shijiu_product_id": readback["shijiu_product_id"],
        "mutation_response_code": (failed.get("response") or {}).get("code"),
        "matching_final_html_mutation_request_count": matching_write_count,
        "mutation_was_not_retried": matching_write_count == 1,
        "expected_good_details_sha256": hashlib.sha256(expected_html.encode("utf-8")).hexdigest(),
        "observed_good_details_sha256": hashlib.sha256(observed_html.encode("utf-8")).hexdigest(),
        "observed_matches_previous_minimal_good_details": observed_html == str(previous_payload.get("good_details") or ""),
        "expected_html_cos_image_count": len(expected_urls),
        "observed_html_image_count": len(observed_urls),
        "expected_html_all_urls_are_uploaded_targets": set(expected_urls).issubset({
            str(row.get("target_url") or "") for row in uploads.values()
            if row.get("status") == "UPLOADED"
        }),
        "expected_html_contains_source_hotlinks": any(
            host in expected_html for host in ("mikihouse.co.jp", "cdn.shopify.com", "img.mksk.me")
        ),
        "observed_broadcast_count": len(readback["carousel_urls"]),
        "observed_good_detail_pics_count": len(readback["detail_image_urls"]),
        "observed_sku_count": readback["sku_count"],
        "all_non_html_fields_match_last_verified_state": True,
        "category_verified": readback["target_category_id"] == TARGET_CATEGORY_ID,
        "readback_snapshot_sha256": content_sha256(snapshot),
        "interpretation": (
            "The single final-HTML native full-payload UPDATE was acknowledged with code 200, "
            "but getFormatInfo retained the exact prior minimal text HTML. The 17 broadcast URLs, "
            "16 ordered detail pictures, all SKU/price/stock/spec/image values, and category remained "
            "unchanged. This is a target persistence failure, not a readback-validator false negative."
        ),
    })
    return result


def build_representative_next_20_plan(
    master: dict[str, Any],
    special: set[str],
    mapping: dict[str, Any],
    category: dict[str, Any],
    prohibited: set[str],
) -> dict[str, Any]:
    from .shijiu_import import _classification_name

    grouped: dict[str, list[dict[str, Any]]] = {key: [] for key in ("footwear", "apparel", "baby", "goods")}
    names = Counter(str(row.get("name") or "").strip() for row in master.get("products") or [])
    for product in master.get("products") or []:
        number = str(product.get("product_number") or "")
        classification = _classification_name(product)
        if (
            classification not in grouped
            or not number
            or number in special
            or number in prohibited
            or not product.get("active")
            or (mapping.get("products", {}).get(number) or {}).get("shijiu_product_id") not in (None, "")
            or names[str(product.get("name") or "").strip()] != 1
        ):
            continue
        item = map_product_to_shijiu(product, category, excluded_product_numbers=special)
        if not item.get("publish_ready"):
            continue
        refs = image_reference_sets(item)
        stages = stage_plan(item)
        metrics = _metrics(product)
        grouped[classification].append({
            "product_number": number,
            "good_name": item["shijiu_payload_preview"]["good_name"],
            "classification": classification,
            "complexity_band": (
                "rich_media" if len(refs["all_detail"]) >= 8 or len(refs["all_broadcast"]) > 12
                else "multi_sku" if metrics["variant_count"] >= 8
                else "simple"
            ),
            "variant_count": metrics["variant_count"],
            "broadcast_count": len(refs["all_broadcast"]),
            "good_detail_pics_count": len(refs["all_detail"]),
            "required_cos_upload_count": len(item["image_upload_plan"]),
            "required_create_count": 1,
            "required_update_count": len(stages) - 1,
            "required_stages": [
                {
                    "sequence": stage["sequence"],
                    "key": stage["key"],
                    "operation": stage["operation"],
                    "broadcast_count": stage["broadcast_count"],
                    "good_detail_pics_count": stage["detail_pic_count"],
                    "new_cos_upload_count": len(stage["new_references"]),
                }
                for stage in stages
            ],
            "payload_sha256": item["payload_sha256"],
        })
    selected: list[dict[str, Any]] = []
    desired = ("simple", "multi_sku", "rich_media", "simple", "rich_media")
    for classification in ("footwear", "apparel", "baby", "goods"):
        pool = grouped[classification]
        picked: set[str] = set()
        for band in desired:
            options = [row for row in pool if row["product_number"] not in picked]
            options.sort(key=lambda row: (
                0 if row["complexity_band"] == band else 1,
                abs(row["variant_count"] - (12 if band == "multi_sku" else 3)),
                abs(row["broadcast_count"] - (16 if band == "rich_media" else 6)),
                row["product_number"],
            ))
            if not options:
                raise LiveImportError(f"insufficient next-20 candidates for {classification}")
            chosen = options[0]
            picked.add(chosen["product_number"])
            selected.append({"sequence": len(selected) + 1, **chosen})
    bands = Counter(row["complexity_band"] for row in selected)
    return {
        "schema_version": 1,
        "generated_at": now(),
        "status": "FROZEN_NOT_EXECUTED",
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "fixed_target_category_id": TARGET_CATEGORY_ID,
        "product_count": len(selected),
        "coverage": {
            "classification_counts": dict(Counter(row["classification"] for row in selected)),
            "complexity_band_counts": dict(bands),
            "includes_simple": bands["simple"] > 0,
            "includes_multi_sku": bands["multi_sku"] > 0,
            "includes_rich_media": bands["rich_media"] > 0,
        },
        "products": selected,
        "execution_authorized": False,
        "real_write_requests": 0,
        "pdf_special_exclusion_count": len(special),
        "legacy_reference_touched": False,
        "sensitive_values_included": False,
    }
