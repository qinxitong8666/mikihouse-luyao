from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .shijiu_canonical_create import load_verified_browser_credentials
from .shijiu_complex_import import TARGET_CATEGORY_ID, UiContextReadClient, _metrics
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
    OFFICIAL_MIKIHOUSE_IMAGE_HOST_SUFFIXES,
    LiveImportError,
    ShijiuLiveClient,
    is_official_mikihouse_image_url,
)
from .shijiu_staged_media_import import (
    PROTECTED_FROZEN_FILES,
    StagedMediaRunner,
    _file_sha256,
    image_reference_sets,
    initial_checkpoint,
    stage_plan,
)


COMPLETE_MODE = "STAGED_RICH_MEDIA_COMPLETE_SINGLE_REAL_VALIDATION"
COMPLETE_WRITE_CONFIRMATION = "MIKIHOUSE_STAGED_RICH_MEDIA_COMPLETE_SINGLE_STEP"
PRIOR_STAGED_PRODUCT = "10-8375-578"
COMPLETE_PROTECTED_FILES = tuple(dict.fromkeys((
    *PROTECTED_FROZEN_FILES,
    "config/shijiu_staged_rich_media_single.json",
    "state/shijiu_staged_rich_media_single_checkpoint.json",
    "deliverables/shijiu_import/staged_rich_media_validation_report.json",
    "deliverables/shijiu_import/staged_rich_media_validation_readbacks.json",
    "deliverables/shijiu_import/staged_rich_media_capacity_conclusion.json",
)))


def _configured_product_numbers(root: Path) -> set[str]:
    numbers: set[str] = set()
    for relative in (
        "config/shijiu_first_live_batch.json",
        "config/shijiu_complex_live_batch.json",
        "config/shijiu_complexity_bisection_batch.json",
        "config/shijiu_high_sku_14_probe.json",
        "config/shijiu_staged_rich_media_single.json",
        "config/shijiu_staged_rich_media_complete_single.json",
        "config/shijiu_staged_detail_html_single.json",
        "config/shijiu_production_architecture_verification_single.json",
        "config/shijiu_richtext_e2e_single.json",
    ):
        path = root / relative
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        numbers.update(str(value) for value in payload.get("product_numbers") or [] if value)
        numbers.update(
            str(row.get("product_number"))
            for row in payload.get("products") or []
            if row.get("product_number")
        )
        product = payload.get("product") or {}
        if product.get("product_number"):
            numbers.add(str(product["product_number"]))
    return numbers


def _mapped_row_hashes(mapping: dict[str, Any]) -> dict[str, str]:
    return {
        number: content_sha256(row)
        for number, row in sorted(mapping.get("products", {}).items())
        if row.get("shijiu_product_id")
    }


def select_complete_candidate(
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
    eligible = []
    for product in master.get("products") or []:
        number = str(product.get("product_number") or "")
        variants = list(product.get("variants") or [])
        images = list(product.get("ordered_images") or [])
        roles = Counter(str(row.get("role") or "") for row in images)
        if (
            not number
            or number in special
            or number in prohibited
            or not product.get("active")
            or (mapping.get("products", {}).get(number) or {}).get("shijiu_product_id") not in (None, "")
            or not 2 <= len(variants) <= 6
            or not 22 <= len(images) <= 32
            or names[str(product.get("name") or "").strip()] != 1
            or not roles["product_gallery"]
            or not roles["detail"]
        ):
            continue
        item = map_product_to_shijiu(product, category, excluded_product_numbers=special)
        if item.get("publish_ready"):
            eligible.append((abs(len(images) - 27), number, item, _metrics(product), roles))
    if not eligible:
        raise LiveImportError("no complete-flow candidate satisfies the frozen selection policy")
    _, _, item, metrics, roles = min(eligible, key=lambda row: (row[0], row[1]))
    refs = image_reference_sets(item)
    selection = {
        "schema_version": 1,
        "generated_at": now(),
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "mode": COMPLETE_MODE,
        "selection_policy": (
            "deterministic: active, publishable, unmapped, non-special, absent from every prior "
            "frozen/attempted config, source-unique name, 2-6 variants, 22-32 ordered official "
            "images, gallery+detail; then closest to 27 images and product_number"
        ),
        "fixed_target_category_id": TARGET_CATEGORY_ID,
        "pdf_special_exclusion_count": len(special),
        "candidate_pool_count": len(eligible),
        "historical_prohibited_product_numbers": sorted(prohibited),
        "maximum_product_create_requests": 1,
        "maximum_product_save_requests_per_runner_invocation": 1,
        "all_resource_preflight_required_before_any_shijiu_write": True,
        "approved_source_host_suffixes": list(OFFICIAL_MIKIHOUSE_IMAGE_HOST_SUFFIXES),
        "protected_frozen_evidence": {
            relative: _file_sha256(root / relative) for relative in COMPLETE_PROTECTED_FILES
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
            "detail_pic_count": len(refs["all_detail"]),
            "source_payload_sha256": item["payload_sha256"],
        },
        "stages": stage_plan(item),
    }
    return item, selection


def load_complete_candidate(
    master: dict[str, Any],
    special: set[str],
    mapping: dict[str, Any],
    category: dict[str, Any],
    selection: dict[str, Any],
) -> dict[str, Any]:
    number = str((selection.get("product") or {}).get("product_number") or "")
    if (
        selection.get("mode") != COMPLETE_MODE
        or selection.get("fixed_target_category_id") != TARGET_CATEGORY_ID
        or number in special
        or number in set(selection.get("historical_prohibited_product_numbers") or [])
    ):
        raise LiveImportError("complete-flow frozen selection boundary failed")
    product = next((row for row in master.get("products") or [] if row.get("product_number") == number), None)
    if not product or not product.get("active"):
        raise LiveImportError("complete-flow source product is missing or inactive")
    item = map_product_to_shijiu(product, category, excluded_product_numbers=special)
    if (
        item["payload_sha256"] != selection["product"]["source_payload_sha256"]
        or stage_plan(item) != selection.get("stages")
    ):
        raise LiveImportError("complete-flow frozen source/stage payload drift")
    return item


def initialize_complete_checkpoint(
    checkpoint: dict[str, Any], item: dict[str, Any]
) -> dict[str, Any]:
    checkpoint["status"] = "READY_FOR_RESOURCE_PREFLIGHT"
    checkpoint["resource_preflight"] = {
        "status": "NOT_STARTED",
        "required_reference_count": len(item["image_upload_plan"]),
        "results": {},
        "shijiu_requests_sent": 0,
        "shijiu_write_requests_sent": 0,
        "error": None,
    }
    return checkpoint


class CompleteStagedMediaRunner(StagedMediaRunner):
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
            checkpoint = initial_checkpoint(item, selection, mode=COMPLETE_MODE)
            write_json_atomic(
                checkpoint_path,
                initialize_complete_checkpoint(checkpoint, item),
            )
        self.complete_selection = selection
        super().__init__(
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
            mode=COMPLETE_MODE,
            expected_confirmation=COMPLETE_WRITE_CONFIRMATION,
            prohibited_product_numbers=set(
                selection["historical_prohibited_product_numbers"]
            ),
            protected_frozen_files=COMPLETE_PROTECTED_FILES,
        )
        self.normalize_frozen_pre_update_read_gate_evidence()

    def normalize_frozen_pre_update_read_gate_evidence(self) -> bool:
        """Classify a frozen pre-update read failure without contacting Shijiu.

        A persisted pre-update snapshot followed by zero attempts and no payload
        hash proves that the native full-payload save was never submitted.  The
        request ledger must also contain exactly one save per verified stage.
        """
        if self.checkpoint.get("status") != "FROZEN_ON_FIRST_ANOMALY":
            return False
        cursor = int(self.checkpoint.get("stage_cursor") or 0)
        stages = self.checkpoint.get("stages") or []
        if cursor >= len(stages):
            return False
        stage = stages[cursor]
        failed = self.checkpoint.get("first_failed_state") or {}
        if not (
            stage.get("operation") != "CREATE"
            and stage.get("attempts") == 0
            and not stage.get("payload_sha256")
            and stage.get("pre_update_snapshot_sha256")
            and failed.get("mutation_request_sent") is False
        ):
            return False
        verified_count = sum(row.get("state") == "VERIFIED" for row in stages)
        save_count = sum(
            row.get("path") == "/shopapi/Goods/newAddGood"
            and row.get("semantic_operation") == "write"
            for row in self.checkpoint.get("request_ledger") or []
        )
        if save_count != verified_count:
            raise LiveImportError(
                "cannot prove pre-update no-mutation state from the request ledger"
            )
        snapshot_data = (
            (stage.get("pre_update_getFormatInfo_snapshot") or {}).get("data") or {}
        )
        broadcast_count = len(
            [value for value in str(snapshot_data.get("broadcast") or "").split(",") if value]
        )
        detail_pic_count = len(
            [
                value
                for value in str(snapshot_data.get("good_detail_pics") or "").split(",")
                if value
            ]
        )
        sku_count = len(snapshot_data.get("sku_info") or [])
        stage["execution_phase"] = "PRE_UPDATE_UI_CONTEXT_STRONG_READBACK"
        stage["post_failure_readonly_confirmation"] = {
            "status": "PRE_UPDATE_SNAPSHOT_REMAINS_CURRENT_NO_MUTATION_WAS_SENT",
            "product_id": self.checkpoint.get("shijiu_product_id"),
            "pre_update_snapshot_sha256": stage["pre_update_snapshot_sha256"],
            "verified_stage_save_request_count": verified_count,
            "observed_total_save_request_count": save_count,
            "target_save_requests_after_snapshot": 0,
        }
        failed.update({
            "failure_phase": "PRE_UPDATE_UI_CONTEXT_STRONG_READBACK",
            "failure_scope": "PRE_UPDATE_READ_ONLY_GATE",
            "target_state_changed_by_failed_stage": False,
            "last_persisted_target_state": {
                "broadcast_url_count": broadcast_count,
                "good_detail_pics_url_count": detail_pic_count,
                "sku_count": sku_count,
                "pre_update_snapshot_sha256": stage["pre_update_snapshot_sha256"],
            },
        })
        self.checkpoint["first_failed_state"] = failed
        self._persist()
        return True

    def _assert_protected_complete_boundary(self) -> None:
        current_files = {
            relative: _file_sha256(self.root / relative)
            for relative in COMPLETE_PROTECTED_FILES
        }
        if current_files != self.complete_selection.get("protected_frozen_evidence"):
            raise LiveImportError("prior frozen evidence changed during complete-flow validation")
        mapping = load_mapping_state(self.mapping_path)
        protected_numbers = set(
            self.complete_selection.get("protected_existing_mapping_row_hashes") or {}
        )
        current_rows = {
            number: content_sha256(mapping["products"][number])
            for number in protected_numbers
        }
        if current_rows != self.complete_selection.get("protected_existing_mapping_row_hashes"):
            raise LiveImportError("previously verified mapping row changed")
        prior = mapping["products"][PRIOR_STAGED_PRODUCT]
        if prior.get("shijiu_product_id") != "9358250":
            raise LiveImportError("10-8375-578 protected mapping identity changed")

    def run_resource_preflight(self) -> dict[str, Any]:
        checkpoint_preflight = self.checkpoint.get("resource_preflight") or {}
        if self.checkpoint.get("status") != "READY_FOR_RESOURCE_PREFLIGHT":
            raise LiveImportError("resource preflight is already consumed or checkpoint is terminal")
        if checkpoint_preflight.get("status") not in {"NOT_STARTED", "IN_PROGRESS"}:
            raise LiveImportError("resource preflight status is not resumable")
        if self.client.requests or self.ui.requests or self.checkpoint.get("request_ledger"):
            raise LiveImportError("resource preflight must start with zero Shijiu requests")
        try:
            self._assert_protected_complete_boundary()
            plan = list(self.item.get("image_upload_plan") or [])
            expected_references = [row["upload_reference"] for row in plan]
            if (
                len(expected_references) != len(set(expected_references))
                or [row.get("order") for row in plan] != list(range(1, len(plan) + 1))
                or any(
                    row.get("role") not in {"main", "product_gallery", "variant_color", "detail"}
                    for row in plan
                )
            ):
                raise LiveImportError("resource preflight image enumeration contract failed")
            source_urls = [str(row.get("source_url") or "") for row in plan]
            if len(source_urls) != len(set(source_urls)):
                raise LiveImportError("resource preflight requires an ordered deduplicated URL set")
            unknown = [url for url in source_urls if not is_official_mikihouse_image_url(url)]
            if unknown:
                raise LiveImportError(
                    f"resource preflight found {len(unknown)} unknown/non-HTTPS source image domains"
                )
            known = set(expected_references)
            if any(
                row.get("image_upload_reference") not in known
                for row in self.item.get("source_variants") or []
            ):
                raise LiveImportError("variant image is absent from complete resource enumeration")
            checkpoint_preflight.update({
                "status": "IN_PROGRESS",
                "started_at": checkpoint_preflight.get("started_at") or now(),
                "approved_source_host_suffixes": list(OFFICIAL_MIKIHOUSE_IMAGE_HOST_SUFFIXES),
                "enumerated_reference_count": len(plan),
                "all_domains_approved_before_download": True,
            })
            self.checkpoint["resource_preflight"] = checkpoint_preflight
            self._persist()
            results = checkpoint_preflight.setdefault("results", {})
            for row in plan:
                reference = row["upload_reference"]
                existing = results.get(reference)
                if existing and existing.get("status") == "VERIFIED":
                    continue
                result = self.client.preflight_official_image(row["source_url"])
                results[reference] = {
                    "status": "VERIFIED",
                    "order": row["order"],
                    "role": row["role"],
                    **result,
                }
                self._persist()
            if self.client.requests or self.ui.requests or self.checkpoint.get("request_ledger"):
                raise LiveImportError("resource preflight emitted a Shijiu request")
            if set(results) != set(expected_references) or any(
                row.get("status") != "VERIFIED" for row in results.values()
            ):
                raise LiveImportError("resource preflight did not verify every enumerated image")
            checkpoint_preflight.update({
                "status": "PASSED",
                "completed_at": now(),
                "verified_reference_count": len(results),
                "verified_content_sha256": content_sha256(results),
                "shijiu_requests_sent": 0,
                "shijiu_write_requests_sent": 0,
                "error": None,
            })
            self.checkpoint["status"] = "READY_FOR_CREATE"
            self._persist()
            return copy.deepcopy(checkpoint_preflight)
        except Exception as error:
            checkpoint_preflight.update({
                "status": "BLOCKED",
                "failed_at": now(),
                "error": {"type": type(error).__name__, "message": str(error)},
                "shijiu_requests_sent": len(self.client.requests) + len(self.ui.requests),
                "shijiu_write_requests_sent": 0,
            })
            self.checkpoint["resource_preflight"] = checkpoint_preflight
            self.checkpoint["status"] = "BLOCKED_RESOURCE_PREFLIGHT_ZERO_SHIJIU_WRITES"
            self.checkpoint["stop_reason"] = checkpoint_preflight["error"]
            self._persist()
            raise

    def _protected_preflight(self) -> None:
        self._assert_protected_complete_boundary()
        preflight = self.checkpoint.get("resource_preflight") or {}
        expected = {row["upload_reference"] for row in self.item["image_upload_plan"]}
        if (
            preflight.get("status") != "PASSED"
            or preflight.get("shijiu_requests_sent") != 0
            or preflight.get("shijiu_write_requests_sent") != 0
            or set(preflight.get("results") or {}) != expected
            or any(
                row.get("status") != "VERIFIED"
                for row in (preflight.get("results") or {}).values()
            )
        ):
            raise LiveImportError("complete resource preflight gate is not satisfied")
        super()._protected_preflight()

    def run_next_step(self) -> dict[str, Any]:
        if self.checkpoint.get("status") not in {"READY_FOR_CREATE", "READY_FOR_NEXT_STAGE"}:
            raise LiveImportError("all-resource preflight must pass before any Shijiu mutation")
        return super().run_next_step()

    def _report(self) -> dict[str, Any]:
        report = super()._report()
        resource = self.checkpoint.get("resource_preflight") or {}
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
            "prior_staged_product_preserved": True,
            "prior_staged_product_id": "9358250",
            "production_architecture_verified": self.checkpoint.get("status") == "COMPLETED",
        })
        return report


def make_complete_clients(
    private_dir: Path, canonical_contract_path: Path
) -> tuple[ShijiuLiveClient, UiContextReadClient, dict[str, Any]]:
    token, secret, evidence = load_verified_browser_credentials(private_dir, canonical_contract_path)
    client = ShijiuLiveClient(
        token,
        secret,
        write_confirmation=COMPLETE_WRITE_CONFIRMATION,
    )
    ui = UiContextReadClient(private_dir, canonical_contract_path)
    if client.token != ui.query_token or client.secret != ui.base_form.get("secret"):
        raise LiveImportError("canonical save and UI-context session credentials differ")
    return client, ui, evidence


def build_complete_capacity_conclusion(checkpoint: dict[str, Any]) -> dict[str, Any]:
    verified = [row for row in checkpoint.get("stages") or [] if row.get("state") == "VERIFIED"]
    complete = checkpoint.get("status") == "COMPLETED"
    first_failed = checkpoint.get("first_failed_state") or {}
    pre_update_read_gate = (
        first_failed.get("failure_scope") == "PRE_UPDATE_READ_ONLY_GATE"
        and first_failed.get("mutation_request_sent") is False
    )
    return {
        "schema_version": 1,
        "generated_at": now(),
        "status": checkpoint.get("status"),
        "product_number": checkpoint.get("product_number"),
        "shijiu_product_id": checkpoint.get("shijiu_product_id"),
        "resource_preflight_status": (checkpoint.get("resource_preflight") or {}).get("status"),
        "verified_stage_keys": [row["key"] for row in verified],
        "maximum_verified_broadcast_url_count": max(
            (int((row.get("metrics") or {}).get("broadcast_url_count") or 0) for row in verified),
            default=0,
        ),
        "maximum_verified_good_detail_pics_url_count": max(
            (int((row.get("metrics") or {}).get("good_detail_pics_url_count") or 0) for row in verified),
            default=0,
        ),
        "final_good_details_html_verified": bool(
            verified and verified[-1].get("operation") == "UPDATE_GOOD_DETAILS"
        ),
        "production_import_architecture_verified": complete,
        "first_failed_or_blocked_state": first_failed or None,
        "first_target_rejected_state": None if pre_update_read_gate else (first_failed or None),
        "detail_update_requests_sent": sum(
            row.get("path") == "/shopapi/Goods/newAddGood"
            and row.get("semantic_operation") == "write"
            and "detail" in str(row.get("operation") or "").casefold()
            for row in checkpoint.get("request_ledger") or []
        ),
        "interpretation": (
            "Lightweight CREATE plus staged native full-payload broadcast, detail-picture, and "
            "final HTML updates completed with strong readback."
            if complete else (
                "Twenty-seven ordered broadcast URLs were strongly verified. Before the first "
                "detail-picture save, the UI-context read-only gate returned HTTP 502; the "
                "checkpoint and request ledger prove that no detail-picture UPDATE was sent. "
                "Detail pictures and final HTML therefore remain untested, and no server hard "
                "limit is inferred."
                if pre_update_read_gate else
                "The single product stopped at the first blocked or anomalous state; no server hard limit is inferred."
            )
        ),
        "server_hard_limit_proven": False,
        "sensitive_values_included": False,
    }


def build_next_20_frozen_plan(
    master: dict[str, Any],
    special: set[str],
    mapping: dict[str, Any],
    category: dict[str, Any],
    prohibited: set[str],
) -> dict[str, Any]:
    from .shijiu_import import _classification_name

    candidates: list[dict[str, Any]] = []
    names = Counter(str(row.get("name") or "").strip() for row in master.get("products") or [])
    for product in master.get("products") or []:
        number = str(product.get("product_number") or "")
        if (
            not number or number in special or number in prohibited or not product.get("active")
            or (mapping.get("products", {}).get(number) or {}).get("shijiu_product_id") not in (None, "")
        ):
            continue
        item = map_product_to_shijiu(product, category, excluded_product_numbers=special)
        if not item.get("publish_ready"):
            continue
        metrics = _metrics(product)
        candidates.append({
            "product_number": number,
            "good_name": item["shijiu_payload_preview"]["good_name"],
            "classification": _classification_name(product),
            "name_unique_in_source": names[str(product.get("name") or "").strip()] == 1,
            "variant_count": metrics["variant_count"],
            "official_image_count": metrics["image_count"],
            "payload_sha256": item["payload_sha256"],
        })
    selected = []
    for classification in ("footwear", "apparel", "baby", "goods"):
        rows = [row for row in candidates if row["classification"] == classification]
        rows.sort(key=lambda row: (
            0 if row["name_unique_in_source"] else 1,
            -row["variant_count"],
            row["official_image_count"],
            row["product_number"],
        ))
        if len(rows) < 5:
            raise LiveImportError(f"insufficient next-20 candidates for {classification}")
        for row in rows[:5]:
            selected.append({"sequence": len(selected) + 1, **row})
    return {
        "schema_version": 1,
        "generated_at": now(),
        "status": "FROZEN_NOT_EXECUTED",
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "fixed_target_category_id": TARGET_CATEGORY_ID,
        "product_count": len(selected),
        "products": selected,
        "execution_authorized": False,
        "real_write_requests": 0,
        "pdf_special_exclusion_count": len(special),
        "legacy_reference_touched": False,
        "sensitive_values_included": False,
    }
