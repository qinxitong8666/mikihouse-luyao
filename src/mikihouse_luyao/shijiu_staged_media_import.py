from __future__ import annotations

import copy
import hashlib
import html
import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .shijiu_canonical_create import load_verified_browser_credentials
from .shijiu_complex_import import (
    TARGET_CATEGORY_ID,
    UiContextReadClient,
    UiStrongReadbackError,
    _metrics,
    ui_precreate_absence,
    ui_strong_readback,
)
from .shijiu_complexity_bisection import ALL_PREVIOUS_CREATE_PRODUCTS
from .shijiu_import import (
    EXPECTED_SPECIAL_COUNT,
    PDF_SPECIAL_EXCLUDED_REASON,
    SOURCE_CODE,
    content_sha256,
    load_mapping_state,
    map_product_to_shijiu,
    now,
    validate_live_mikihouse_category,
    write_json_atomic,
)
from .shijiu_live_import import (
    CREATE_PATH,
    DETAIL_PATH,
    IMAGE_UPLOAD_PATH,
    DuplicateRiskError,
    LiveImportError,
    ShijiuLiveClient,
    _redacted_response,
    persist_verified_mapping,
    validate_canonical_create_payload,
    validate_canonical_update_payload,
)


MODE = "STAGED_RICH_MEDIA_SINGLE_REAL_VALIDATION"
WRITE_CONFIRMATION = "MIKIHOUSE_STAGED_RICH_MEDIA_SINGLE_STEP"
PLACEHOLDER = re.compile(r"\{\{SHIJIU_COS_URL:([^}]+)}}")
PERMANENTLY_PROHIBITED = {
    *ALL_PREVIOUS_CREATE_PRODUCTS,
    "00-4000-057",
    "63-6602-492",
}
PROTECTED_FROZEN_FILES = (
    "state/shijiu_canonical_create_checkpoint.json",
    "state/shijiu_complex_live_batch_checkpoint.json",
    "state/shijiu_complexity_bisection_checkpoint.json",
    "state/shijiu_high_sku_14_probe_checkpoint.json",
    "deliverables/shijiu_import/complex_live_batch_report.json",
    "deliverables/shijiu_import/complexity_bisection_report.json",
    "deliverables/shijiu_import/high_sku_14_probe_report.json",
)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _references(value: Any) -> list[str]:
    serialized = json.dumps(value, ensure_ascii=False)
    return list(dict.fromkeys(PLACEHOLDER.findall(serialized)))


def _split_urls(value: Any) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _resolve(value: Any, uploads: dict[str, dict[str, Any]]) -> Any:
    target = {
        reference: row.get("target_url")
        for reference, row in uploads.items()
        if row.get("status") == "UPLOADED" and row.get("target_url")
    }

    def replace(child: Any) -> Any:
        if isinstance(child, dict):
            return {key: replace(item) for key, item in child.items()}
        if isinstance(child, list):
            return [replace(item) for item in child]
        if isinstance(child, str):
            return PLACEHOLDER.sub(
                lambda match: str(target.get(match.group(1)) or match.group(0)), child
            )
        return child

    result = replace(copy.deepcopy(value))
    if "SHIJIU_COS_URL" in json.dumps(result, ensure_ascii=False):
        raise LiveImportError("staged payload contains an unresolved COS reference")
    serialized = json.dumps(result, ensure_ascii=False)
    if any(host in serialized for host in ("cdn.shopify.com", "mikihouse.co.jp", "img.mksk.me")):
        raise LiveImportError("official MIKI HOUSE hotlink leaked into staged payload")
    return result


def _minimal_text_details(item: dict[str, Any]) -> str:
    name = html.escape(str(item["shijiu_payload_preview"]["good_name"]))
    number = html.escape(str(item["product_number"]))
    return f'<section data-source="MIKIHOUSE"><h2>{name}</h2><p>品番：{number}</p></section>'


def _placeholder_for(reference: str) -> str:
    return f"{{{{SHIJIU_COS_URL:{reference}}}}}"


def image_reference_sets(item: dict[str, Any]) -> dict[str, list[str]]:
    preview = item["shijiu_payload_preview"]
    all_broadcast = _references(preview.get("broadcast"))
    detail = _references(preview.get("good_detail_pics"))
    create_broadcast = all_broadcast[:4]
    create_required = list(dict.fromkeys(
        _references(preview.get("master_graph"))
        + create_broadcast
        + _references(preview.get("sku_info"))
    ))
    return {
        "all_broadcast": all_broadcast,
        "all_detail": detail,
        "create_broadcast": create_broadcast,
        "create_required": create_required,
    }


def stage_plan(item: dict[str, Any]) -> list[dict[str, Any]]:
    refs = image_reference_sets(item)
    stages: list[dict[str, Any]] = [{
        "sequence": 1,
        "key": "CREATE_CORE",
        "operation": "CREATE",
        "broadcast_count": len(refs["create_broadcast"]),
        "detail_pic_count": 0,
        "new_references": refs["create_required"],
    }]
    current = len(refs["create_broadcast"])
    while current < len(refs["all_broadcast"]):
        next_count = min(current + 8, len(refs["all_broadcast"]))
        stages.append({
            "sequence": len(stages) + 1,
            "key": f"BROADCAST_{current + 1}_{next_count}",
            "operation": "UPDATE_BROADCAST",
            "broadcast_count": next_count,
            "detail_pic_count": 0,
            "new_references": refs["all_broadcast"][current:next_count],
        })
        current = next_count
    current = 0
    while current < len(refs["all_detail"]):
        next_count = min(current + 8, len(refs["all_detail"]))
        stages.append({
            "sequence": len(stages) + 1,
            "key": f"DETAIL_PICS_{current + 1}_{next_count}",
            "operation": "UPDATE_DETAIL_PICS",
            "broadcast_count": len(refs["all_broadcast"]),
            "detail_pic_count": next_count,
            "new_references": refs["all_detail"][current:next_count],
        })
        current = next_count
    stages.append({
        "sequence": len(stages) + 1,
        "key": "FINAL_GOOD_DETAILS_HTML",
        "operation": "UPDATE_GOOD_DETAILS",
        "broadcast_count": len(refs["all_broadcast"]),
        "detail_pic_count": len(refs["all_detail"]),
        "new_references": [],
    })
    return stages


def build_stage_payload(
    item: dict[str, Any],
    stage: dict[str, Any],
    uploads: dict[str, dict[str, Any]],
    *,
    product_id: str | None = None,
) -> dict[str, Any]:
    refs = image_reference_sets(item)
    payload = copy.deepcopy(item["shijiu_payload_preview"])
    payload["state"] = "1"
    payload["is_shelf"] = 0
    payload["broadcast"] = ",".join(
        _placeholder_for(reference)
        for reference in refs["all_broadcast"][: int(stage["broadcast_count"])]
    )
    payload["good_detail_pics"] = ",".join(
        _placeholder_for(reference)
        for reference in refs["all_detail"][: int(stage["detail_pic_count"])]
    )
    if stage["operation"] != "UPDATE_GOOD_DETAILS":
        payload["good_details"] = _minimal_text_details(item)
    payload = _resolve(payload, uploads)
    if product_id is not None:
        payload["id"] = int(product_id)
        validate_canonical_update_payload(payload)
    else:
        validate_canonical_create_payload(payload)
    return payload


def select_staged_media_candidate(
    root: Path,
    master: dict[str, Any],
    special: set[str],
    mapping: dict[str, Any],
    category: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if len(special) != EXPECTED_SPECIAL_COUNT:
        raise LiveImportError("permanent PDF special exclusion count changed")
    names = Counter(str(row.get("name") or "").strip() for row in master.get("products") or [])
    eligible = []
    for product in master.get("products") or []:
        number = str(product.get("product_number") or "")
        variants = list(product.get("variants") or [])
        images = list(product.get("ordered_images") or [])
        roles = Counter(str(row.get("role") or "") for row in images)
        if (
            not number
            or number in special
            or number in PERMANENTLY_PROHIBITED
            or not product.get("active")
            or (mapping.get("products", {}).get(number) or {}).get("shijiu_product_id") not in (None, "")
            or not 2 <= len(variants) <= 6
            or not 20 <= len(images) <= 35
            or names[str(product.get("name") or "").strip()] != 1
            or not roles["product_gallery"]
            or not roles["detail"]
        ):
            continue
        item = map_product_to_shijiu(product, category, excluded_product_numbers=special)
        if not item.get("publish_ready"):
            continue
        metrics = _metrics(product)
        eligible.append((abs(len(images) - 27), number, product, item, metrics, roles))
    if not eligible:
        raise LiveImportError("no unique-name 2-6-variant rich-media candidate in the 20-35 image band")
    _, _, product, item, metrics, roles = min(eligible, key=lambda row: (row[0], row[1]))
    stages = stage_plan(item)
    selection = {
        "schema_version": 1,
        "generated_at": now(),
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "mode": MODE,
        "selection_policy": (
            "deterministic: active, publishable, unmapped, non-special, never-attempted, "
            "source-unique name, 2-6 variants, 20-35 ordered official images, gallery+detail; "
            "then closest to 27 images and product_number"
        ),
        "fixed_target_category_id": TARGET_CATEGORY_ID,
        "pdf_special_exclusion_count": len(special),
        "candidate_pool_count": len(eligible),
        "maximum_product_create_requests": 1,
        "maximum_product_save_requests_per_runner_invocation": 1,
        "hard_prohibited_products": sorted(PERMANENTLY_PROHIBITED),
        "protected_frozen_evidence": {
            relative: _file_sha256(root / relative) for relative in PROTECTED_FROZEN_FILES
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
            "detail_pic_count": len(image_reference_sets(item)["all_detail"]),
            "source_payload_sha256": item["payload_sha256"],
        },
        "stages": stages,
    }
    return item, selection


def load_frozen_candidate(
    root: Path,
    master: dict[str, Any],
    special: set[str],
    mapping: dict[str, Any],
    category: dict[str, Any],
    selection: dict[str, Any],
) -> dict[str, Any]:
    number = str((selection.get("product") or {}).get("product_number") or "")
    if (
        selection.get("mode") != MODE
        or selection.get("fixed_target_category_id") != TARGET_CATEGORY_ID
        or number in special
        or number in PERMANENTLY_PROHIBITED
    ):
        raise LiveImportError("frozen staged-media selection boundary failed")
    product = next((row for row in master.get("products") or [] if row.get("product_number") == number), None)
    if not product or not product.get("active"):
        raise LiveImportError("frozen staged-media source product is missing or inactive")
    item = map_product_to_shijiu(product, category, excluded_product_numbers=special)
    if item["payload_sha256"] != selection["product"]["source_payload_sha256"]:
        raise LiveImportError("frozen staged-media source payload drift")
    if stage_plan(item) != selection.get("stages"):
        raise LiveImportError("frozen staged-media stage plan drift")
    return item


def initial_checkpoint(item: dict[str, Any], selection: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "mode": MODE,
        "created_at": now(),
        "updated_at": now(),
        "status": "READY_FOR_CREATE",
        "product_number": item["product_number"],
        "source_payload_sha256": item["payload_sha256"],
        "selection_sha256": content_sha256(selection),
        "stage_cursor": 0,
        "stages": [
            {**copy.deepcopy(stage), "state": "PLANNED", "attempts": 0}
            for stage in selection["stages"]
        ],
        "image_uploads": {},
        "create_response": None,
        "shijiu_product_id": None,
        "mapping_persisted": False,
        "last_verified_payload_sha256": None,
        "last_verified_state": None,
        "first_failed_state": None,
        "request_ledger": [],
        "stop_reason": None,
        "legacy_reference_touched": False,
        "bulk_20_generated_or_executed": False,
    }


def payload_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    business = {key: value for key, value in payload.items() if key != "id"}
    details = str(business.get("good_details") or "")
    return {
        "payload_utf8_bytes": len(json.dumps(business, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
        "sku_count": len(business.get("sku_info") or []),
        "spec_dimension_count": len(business.get("spec_name") or []),
        "broadcast_url_count": len(_split_urls(business.get("broadcast"))),
        "broadcast_characters": len(str(business.get("broadcast") or "")),
        "good_detail_pics_url_count": len(_split_urls(business.get("good_detail_pics"))),
        "good_detail_pics_characters": len(str(business.get("good_detail_pics") or "")),
        "good_details_characters": len(details),
        "good_details_utf8_bytes": len(details.encode("utf-8")),
        "good_details_sha256": hashlib.sha256(details.encode("utf-8")).hexdigest(),
    }


def build_capacity_conclusion(checkpoint: dict[str, Any]) -> dict[str, Any]:
    verified = [row for row in checkpoint.get("stages") or [] if row.get("state") == "VERIFIED"]
    failed = next(
        (row for row in checkpoint.get("stages") or [] if row.get("state") == "FROZEN_ON_ANOMALY"),
        None,
    )
    verified_broadcast = [
        int((row.get("metrics") or {}).get("broadcast_url_count") or 0) for row in verified
    ]
    verified_details = [
        int((row.get("metrics") or {}).get("good_detail_pics_url_count") or 0) for row in verified
    ]
    first_failed = checkpoint.get("first_failed_state") or {}
    target_save_sent = bool(first_failed.get("mutation_request_sent"))
    return {
        "schema_version": 1,
        "generated_at": now(),
        "status": checkpoint.get("status"),
        "product_number": checkpoint.get("product_number"),
        "shijiu_product_id": checkpoint.get("shijiu_product_id"),
        "observed_stable_success": {
            "maximum_ordered_broadcast_url_count": max(verified_broadcast, default=0),
            "maximum_ordered_good_detail_pics_url_count": max(verified_details, default=0),
            "verified_stage_keys": [row["key"] for row in verified],
        },
        "first_failed_or_blocked_state": first_failed or None,
        "first_target_rejected_state": (
            {
                "stage": failed.get("key"),
                "broadcast_count": failed.get("broadcast_count"),
                "detail_pic_count": failed.get("detail_pic_count"),
            }
            if failed and target_save_sent else None
        ),
        "unverified_planned_state": (
            {
                "stage": failed.get("key"),
                "broadcast_count": failed.get("broadcast_count"),
                "detail_pic_count": failed.get("detail_pic_count"),
                "reason": (failed.get("error") or {}).get("message"),
            }
            if failed else None
        ),
        "interpretation": (
            "CREATE with 4 carousel URLs and native full-payload updates through 12 and 20 "
            "ordered carousel URLs were strongly verified. The planned 27-URL update was never "
            "sent because a newly encountered official detail-CDN host was blocked locally before "
            "download/upload. Detail-pic and final-HTML capacities were therefore not tested."
        ),
        "observed_empirical_boundary_only": True,
        "server_hard_limit_proven": False,
        "sensitive_values_included": False,
    }


def build_plan_validation(
    original_plan: dict[str, Any], selection: dict[str, Any], checkpoint: dict[str, Any]
) -> dict[str, Any]:
    result = copy.deepcopy(original_plan)
    result.update({
        "generated_at": now(),
        "status": (
            "PARTIALLY_VALIDATED_FROZEN" if checkpoint.get("status") == "FROZEN_ON_FIRST_ANOMALY"
            else checkpoint.get("status")
        ),
        "execution_authorized": True,
        "further_execution_authorized": False,
        "validation_product_number": checkpoint.get("product_number"),
        "validation_product_id": checkpoint.get("shijiu_product_id"),
        "validation_stage_count": len(selection.get("stages") or []),
        "verified_stage_keys": [
            row["key"] for row in checkpoint.get("stages") or [] if row.get("state") == "VERIFIED"
        ],
        "last_verified_success_state": checkpoint.get("last_verified_state"),
        "first_failed_state": checkpoint.get("first_failed_state"),
        "update_requests_sent": sum(
            row.get("path") == CREATE_PATH
            and "update" in str(row.get("operation") or "").casefold()
            for row in checkpoint.get("request_ledger") or []
        ),
        "continued_after_first_anomaly": False,
        "automatic_retry_or_rollback": False,
        "server_hard_limit_proven": False,
        "legacy_cleanup_executed": False,
        "bulk_20_executed": False,
        "sensitive_values_included": False,
    })
    return result


def make_clients(
    private_dir: Path, canonical_contract_path: Path
) -> tuple[ShijiuLiveClient, UiContextReadClient, dict[str, Any]]:
    token, secret, evidence = load_verified_browser_credentials(private_dir, canonical_contract_path)
    client = ShijiuLiveClient(token, secret, write_confirmation=WRITE_CONFIRMATION)
    ui = UiContextReadClient(private_dir, canonical_contract_path)
    if client.token != ui.query_token or client.secret != ui.base_form.get("secret"):
        raise LiveImportError("canonical save and UI-context session credentials differ")
    return client, ui, evidence


class StagedMediaRunner:
    """Advance exactly one mutation stage per invocation, fail closed forever."""

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
        self.client, self.ui, self.item = client, ui, item
        self.special, self.category, self.selection = special, category, selection
        self.root, self.checkpoint_path, self.mapping_path = root, checkpoint_path, mapping_path
        self.report_path, self.readbacks_path, self.confirmation = report_path, readbacks_path, confirmation
        self.checkpoint = (
            json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if checkpoint_path.exists() else initial_checkpoint(item, selection)
        )
        if (
            self.checkpoint.get("mode") != MODE
            or self.checkpoint.get("product_number") != item["product_number"]
            or self.checkpoint.get("selection_sha256") != content_sha256(selection)
        ):
            raise LiveImportError("staged-media checkpoint identity drift")
        self._client_cursor = self._ui_cursor = 0
        self._normalize_pre_request_upload_block()
        self._persist()

    def _normalize_pre_request_upload_block(self) -> None:
        """Distinguish a local host-policy refusal from an ambiguous upload.

        The upload client records its ledger entry only after an official image
        has downloaded and a multipart request is ready.  Therefore absence of
        the matching source hash proves no COS request was sent.
        """
        if self.checkpoint.get("status") != "FROZEN_ON_FIRST_ANOMALY":
            return
        error = self.checkpoint.get("stop_reason") or {}
        if "image source is not an official HTTPS MIKI HOUSE host" not in str(error.get("message") or ""):
            return
        sent_hashes = {
            row.get("source_url_sha256")
            for row in self.checkpoint.get("request_ledger") or []
            if row.get("path") == IMAGE_UPLOAD_PATH
        }
        changed = False
        for upload in self.checkpoint.get("image_uploads", {}).values():
            if (
                upload.get("status") == "UPLOAD_RESULT_UNKNOWN"
                and upload.get("source_url_sha256") not in sent_hashes
            ):
                upload["status"] = "BLOCKED_BEFORE_DOWNLOAD_OR_UPLOAD_REQUEST"
                upload["target_upload_request_sent"] = False
                changed = True
        if changed:
            failed = self.checkpoint["stages"][int(self.checkpoint["stage_cursor"])]
            failed["post_failure_readonly_confirmation"] = {
                "status": "PRE_UPDATE_SNAPSHOT_REMAINS_CURRENT_NO_MUTATION_WAS_SENT",
                "product_id": self.checkpoint.get("shijiu_product_id"),
                "pre_update_snapshot_sha256": failed.get("pre_update_snapshot_sha256"),
                "target_mutations_after_snapshot": 0,
            }
            self.checkpoint["first_failed_state"].update({
                "failure_scope": "LOCAL_SOURCE_HOST_POLICY_BEFORE_DOWNLOAD_UPLOAD_OR_UPDATE",
                "target_upload_request_sent": False,
            })

    def _ledger(self) -> list[dict[str, Any]]:
        return self.checkpoint.get("request_ledger") or []

    def _report(self) -> dict[str, Any]:
        ledger = self._ledger()
        stages = self.checkpoint["stages"]
        return {
            "schema_version": 1,
            "generated_at": now(),
            "mode": MODE,
            "status": self.checkpoint["status"],
            "source": SOURCE_CODE,
            "target": "SHIJIU",
            "product": self.selection["product"],
            "shijiu_product_id": self.checkpoint.get("shijiu_product_id"),
            "fixed_target_category_id": TARGET_CATEGORY_ID,
            "price_rule": "mini_program_price_jpy=ceil(tax_included_price_jpy*0.65)",
            "currency": "JPY",
            "currency_conversion_applied": False,
            "stage_results": stages,
            "last_verified_success_state": self.checkpoint.get("last_verified_state"),
            "first_failed_state": self.checkpoint.get("first_failed_state"),
            "capacity_interpretation": "observed empirical boundary only; no server hard limit inferred",
            "server_hard_limit_proven": False,
            "request_counts": {
                "read": sum(row.get("semantic_operation") == "read" for row in ledger),
                "write": sum(row.get("semantic_operation") == "write" for row in ledger),
                "image_upload": sum(row.get("path") == IMAGE_UPLOAD_PATH for row in ledger),
                "create": sum(
                    row.get("path") == CREATE_PATH and "create" in str(row.get("operation") or "").casefold()
                    for row in ledger
                ),
                "update": sum(
                    row.get("path") == CREATE_PATH and "update" in str(row.get("operation") or "").casefold()
                    for row in ledger
                ),
            },
            "mapping_persisted": self.checkpoint.get("mapping_persisted", False),
            "shijiu_sku_id_policy": "nullable; target identity is product_id + exact backend_sku_code",
            "pdf_special_exclusion_count": len(self.special),
            "pdf_special_selected": self.item["product_number"] in self.special,
            "historical_frozen_products_retried_or_modified": False,
            "legacy_reference_touched": False,
            "bulk_20_generated_or_executed": False,
            "automatic_retry_or_rollback": False,
            "sensitive_values_included": False,
        }

    def _persist(self) -> None:
        self.checkpoint.setdefault("request_ledger", []).extend(
            copy.deepcopy(self.client.requests[self._client_cursor:])
        )
        self._client_cursor = len(self.client.requests)
        self.checkpoint["request_ledger"].extend(copy.deepcopy(self.ui.requests[self._ui_cursor:]))
        self._ui_cursor = len(self.ui.requests)
        self.checkpoint["updated_at"] = now()
        write_json_atomic(self.checkpoint_path, self.checkpoint)
        write_json_atomic(self.report_path, self._report())
        verified = [stage.get("readback") for stage in self.checkpoint["stages"] if stage.get("state") == "VERIFIED"]
        write_json_atomic(self.readbacks_path, {
            "schema_version": 1,
            "generated_at": now(),
            "product_number": self.item["product_number"],
            "results": verified,
            "verified_stage_count": len(verified),
            "sensitive_values_included": False,
        })

    def _protected_preflight(self) -> None:
        if len(self.special) != EXPECTED_SPECIAL_COUNT or self.item["product_number"] in self.special:
            raise LiveImportError(f"{PDF_SPECIAL_EXCLUDED_REASON}: staged candidate rejected")
        if self.item["product_number"] in PERMANENTLY_PROHIBITED:
            raise LiveImportError("historical attempted/verified product entered staged validation")
        expected = self.selection.get("protected_frozen_evidence") or {}
        actual = {relative: _file_sha256(self.root / relative) for relative in PROTECTED_FROZEN_FILES}
        if actual != expected:
            raise LiveImportError("protected historical checkpoint/report changed")
        validate_live_mikihouse_category(self.category, self.client.categories())
        mapping = load_mapping_state(self.mapping_path)
        row = mapping["products"][self.item["product_number"]]
        product_id = self.checkpoint.get("shijiu_product_id")
        if product_id:
            if str(row.get("shijiu_product_id")) != str(product_id):
                raise DuplicateRiskError("checkpoint/mapping product identity mismatch")
        elif row.get("shijiu_product_id") not in (None, ""):
            raise DuplicateRiskError("candidate became mapped before CREATE")

    def _upload_missing(self, stage: dict[str, Any]) -> None:
        by_reference = {row["upload_reference"]: row for row in self.item["image_upload_plan"]}
        for reference in stage["new_references"]:
            existing = self.checkpoint["image_uploads"].get(reference)
            if existing and existing.get("status") == "UPLOADED":
                continue
            if existing:
                raise DuplicateRiskError(f"ambiguous image upload cannot be retried: {reference}")
            source = by_reference[reference]
            row = {
                "upload_reference": reference,
                "order": source["order"],
                "role": source["role"],
                "source_url_sha256": hashlib.sha256(source["source_url"].encode()).hexdigest(),
                "status": "UPLOAD_INTENT_PERSISTED",
                "intent_at": now(),
            }
            self.checkpoint["image_uploads"][reference] = row
            self._persist()
            try:
                target_url, response = self.client.upload_image(source["source_url"], confirmation=self.confirmation)
            except Exception:
                row["status"] = "UPLOAD_RESULT_UNKNOWN"
                self._persist()
                raise
            row.update({
                "status": "UPLOADED",
                "target_url": target_url,
                "response": _redacted_response(response),
                "completed_at": now(),
            })
            self._persist()

    def _exact_readback(self, payload: dict[str, Any], response: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        return ui_strong_readback(
            self.ui,
            self.item,
            payload,
            response,
            require_exact_good_details=True,
        )

    def _readonly_confirm_after_failure(self) -> dict[str, Any]:
        product_id = self.checkpoint.get("shijiu_product_id")
        result: dict[str, Any] = {"performed_at": now(), "target_mutations": 0, "product_id": product_id}
        try:
            rows, evidence = self.ui.exact_name_candidates(
                self.item["shijiu_payload_preview"]["good_name"]
            )
            snapshots = []
            for row in rows:
                candidate_id = str(row.get("id") or row.get("good_id") or row.get("goods_id") or "")
                snapshots.append({
                    "product_id": candidate_id,
                    "getFormatInfo": _redacted_response(self.ui.product_detail(candidate_id)),
                })
            result.update({"status": "READ_ONLY_CONFIRMED", "discovery": evidence, "snapshots": snapshots})
        except Exception as error:
            result.update({"status": "READ_ONLY_CONFIRMATION_FAILED", "error": {"type": type(error).__name__, "message": str(error)}})
        return result

    def _freeze(self, stage: dict[str, Any], error: Exception, *, after_update: bool) -> None:
        stage["state"] = "FROZEN_ON_ANOMALY"
        stage["error"] = {"type": type(error).__name__, "message": str(error), "at": now()}
        self.checkpoint["status"] = "FROZEN_ON_FIRST_ANOMALY"
        self.checkpoint["stop_reason"] = stage["error"]
        self.checkpoint["first_failed_state"] = {
            "stage": stage["key"],
            "operation": stage["operation"],
            "planned_broadcast_count": stage["broadcast_count"],
            "planned_detail_pic_count": stage["detail_pic_count"],
            "mutation_request_sent": after_update,
        }
        self._persist()
        if after_update:
            stage["post_failure_readonly_confirmation"] = self._readonly_confirm_after_failure()
            self._persist()

    def confirm_frozen_state_read_only(self) -> dict[str, Any]:
        """Strongly confirm the last verified payload without any mutation call."""
        if self.checkpoint.get("status") != "FROZEN_ON_FIRST_ANOMALY":
            raise LiveImportError("read-only frozen confirmation requires a frozen checkpoint")
        cursor = int(self.checkpoint["stage_cursor"])
        if cursor < 1:
            raise LiveImportError("no prior verified stage is available")
        writes_before = sum(row.get("semantic_operation") == "write" for row in self._ledger())
        previous_stage = self.checkpoint["stages"][cursor - 1]
        payload = build_stage_payload(self.item, previous_stage, self.checkpoint["image_uploads"])
        readback, discovery = self._exact_readback(payload, {})
        result = {
            "status": "LAST_VERIFIED_STATE_READ_ONLY_RECONFIRMED",
            "performed_at": now(),
            "product_id": readback["shijiu_product_id"],
            "verified_stage": previous_stage["key"],
            "verified_broadcast_count": len(readback["carousel_urls"]),
            "verified_detail_pic_count": len(readback["detail_image_urls"]),
            "verified_sku_count": readback["sku_count"],
            "readback": readback,
            "discovery": discovery,
            "target_mutations": 0,
        }
        failed_stage = self.checkpoint["stages"][cursor]
        failed_stage["post_failure_readonly_confirmation"] = result
        self._persist()
        writes_after = sum(row.get("semantic_operation") == "write" for row in self._ledger())
        if writes_after != writes_before:
            raise LiveImportError("read-only frozen confirmation unexpectedly recorded a write")
        return result

    def run_next_step(self) -> dict[str, Any]:
        if self.confirmation != WRITE_CONFIRMATION:
            raise LiveImportError("exact staged-media single-step write confirmation missing")
        if self.checkpoint["status"] in {"FROZEN_ON_FIRST_ANOMALY", "COMPLETED"}:
            raise LiveImportError("staged-media checkpoint is terminal; retry is forbidden")
        if any(
            stage.get("state") in {"MUTATION_INTENT_PERSISTED", "MUTATION_RESULT_UNKNOWN"}
            for stage in self.checkpoint["stages"]
        ):
            raise DuplicateRiskError("ambiguous staged mutation cannot be retried")
        cursor = int(self.checkpoint["stage_cursor"])
        if cursor >= len(self.checkpoint["stages"]):
            raise LiveImportError("no staged mutation remains")
        stage = self.checkpoint["stages"][cursor]
        if stage["state"] != "PLANNED" or stage["attempts"] != 0:
            raise DuplicateRiskError("next staged mutation was already consumed")
        try:
            self._protected_preflight()
            if stage["operation"] == "CREATE":
                stage["precreate_ui_absence"] = ui_precreate_absence(self.ui, self.item)
            else:
                product_id = str(self.checkpoint.get("shijiu_product_id") or "")
                if not product_id:
                    raise LiveImportError("UPDATE requires a strongly verified CREATE mapping")
                previous_stage = self.checkpoint["stages"][cursor - 1]
                previous_payload = build_stage_payload(
                    self.item, previous_stage, self.checkpoint["image_uploads"], product_id=product_id
                )
                # Complete getFormatInfo snapshot is persisted before any upload or edit.
                snapshot = self.ui.product_detail(product_id)
                stage["pre_update_getFormatInfo_snapshot"] = _redacted_response(snapshot)
                stage["pre_update_snapshot_sha256"] = content_sha256(snapshot)
                self._persist()
                self._exact_readback({key: value for key, value in previous_payload.items() if key != "id"}, {})
            self._upload_missing(stage)
            product_id = self.checkpoint.get("shijiu_product_id")
            payload = build_stage_payload(
                self.item,
                stage,
                self.checkpoint["image_uploads"],
                product_id=str(product_id) if product_id else None,
            )
            stage.update({
                "state": "MUTATION_INTENT_PERSISTED",
                "attempts": 1,
                "intent_at": now(),
                "payload_sha256": content_sha256(payload),
                "metrics": payload_metrics(payload),
            })
            self._persist()
            try:
                if stage["operation"] == "CREATE":
                    response = self.client.create_product_native(payload, confirmation=self.confirmation)
                else:
                    response = self.client.update_product_native(payload, confirmation=self.confirmation)
            except Exception:
                stage["state"] = "MUTATION_RESULT_UNKNOWN"
                self._persist()
                raise
            stage["response"] = _redacted_response(response)
            stage["state"] = "MUTATION_RESPONSE_RECEIVED"
            self._persist()
            time.sleep(2)
            business_payload = {key: value for key, value in payload.items() if key != "id"}
            try:
                readback, discovery = self._exact_readback(business_payload, response)
            except UiStrongReadbackError as error:
                stage["ui_readback_discovery"] = error.evidence
                raise
            if product_id and str(readback["shijiu_product_id"]) != str(product_id):
                raise DuplicateRiskError("UPDATE readback changed the stable Shijiu product id")
            stage.update({
                "state": "VERIFIED",
                "readback": readback,
                "ui_readback_discovery": discovery,
                "verified_at": readback["verified_at"],
            })
            self.checkpoint["shijiu_product_id"] = readback["shijiu_product_id"]
            persist_verified_mapping(self.mapping_path, self.item, readback, content_sha256(business_payload))
            self.checkpoint["mapping_persisted"] = True
            self.checkpoint["last_verified_payload_sha256"] = content_sha256(business_payload)
            self.checkpoint["last_verified_state"] = {
                "stage": stage["key"],
                "operation": stage["operation"],
                **stage["metrics"],
            }
            self.checkpoint["stage_cursor"] = cursor + 1
            self.checkpoint["status"] = (
                "COMPLETED" if cursor + 1 == len(self.checkpoint["stages"])
                else "READY_FOR_NEXT_STAGE"
            )
            self.checkpoint["stop_reason"] = None
            self._persist()
            return self._report()
        except Exception as error:
            mutation_sent = stage.get("state") in {
                "MUTATION_RESPONSE_RECEIVED", "MUTATION_RESULT_UNKNOWN"
            }
            self._freeze(stage, error, after_update=mutation_sent and stage["operation"] != "CREATE")
            raise
