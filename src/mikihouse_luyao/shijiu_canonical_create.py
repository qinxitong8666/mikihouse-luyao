from __future__ import annotations

import copy
import hashlib
import json
import re
import time
import urllib.parse
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
    validate_live_mikihouse_category,
    write_json_atomic,
)
from .shijiu_live_import import (
    ContractMismatchError,
    DuplicateRiskError,
    LiveImportError,
    ShijiuLiveClient,
    _product_id_from_value,
    _redacted_response,
    _resolve_payload,
    _unique_exact_product_matches,
    persist_verified_mapping,
    validate_canonical_create_payload,
    validate_product_readback,
)
from .shijiu_minimal_probe import TARGET_CATEGORY, select_minimal_probe_candidate


CANONICAL_CREATE_CONFIRMATION = "MIKIHOUSE_CANONICAL_CREATE_VALIDATE_ONE"
PREVIOUSLY_TESTED_PRODUCTS = {"00-1000-028", "17-1366-244"}


def load_verified_browser_credentials(
    private_dir: Path, canonical_contract_path: Path
) -> tuple[str, str, dict[str, Any]]:
    """Load credentials in memory from the verified private capture; never persist them."""
    candidates = sorted(
        private_dir.glob("shijiu-browser-exact-*.private.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise LiveImportError("no private browser-exact capture found")
    capture_path = candidates[0]
    raw_bytes = capture_path.read_bytes()
    raw = json.loads(raw_bytes)
    contract = json.loads(canonical_contract_path.read_text(encoding="utf-8"))
    capture_hash = hashlib.sha256(raw_bytes).hexdigest()
    if capture_hash != contract.get("browser_exact_private_evidence_sha256"):
        raise LiveImportError("private browser capture does not match the canonical contract hash")
    payload = json.loads(raw.get("playwright_request", {}).get("post_data") or "{}")
    if payload.get("id") not in (None, "", 0, "0"):
        raise LiveImportError("canonical private evidence is not a CREATE request")
    readback = raw.get("readback") or {}
    if not (
        readback.get("goods_index_unique")
        and readback.get("get_format_info_product_verified")
        and readback.get("sku_structure_verified")
        and readback.get("sku_code_verified")
    ):
        raise LiveImportError("canonical CREATE private evidence lacks persistence proof")
    token = str(payload.get("token") or "")
    secret = str(payload.get("secret") or "")
    request_url = str(raw.get("playwright_request", {}).get("url") or "")
    token_match = re.search(r"[?&]token=([^&#]+)", request_url)
    query_token = urllib.parse.unquote(token_match.group(1)) if token_match else ""
    if not token or not secret or query_token != token:
        raise LiveImportError("canonical browser credentials are incomplete or inconsistent")
    return token, secret, {
        "private_evidence_sha256": capture_hash,
        "operation_kind": "CREATE",
        "persistence_verified": True,
        "credential_values_persisted": False,
    }


def load_single_candidate(
    master_path: Path,
    special_path: Path,
    mapping_path: Path,
) -> tuple[dict[str, Any], set[str], dict[str, Any]]:
    special = set(read_product_numbers(special_path))
    if len(special) != EXPECTED_SPECIAL_COUNT:
        raise LiveImportError(f"expected {EXPECTED_SPECIAL_COUNT} {PDF_SPECIAL_EXCLUDED_REASON} rows")
    master = json.loads(master_path.read_text(encoding="utf-8"))
    mapping = load_mapping_state(mapping_path)
    product, selection = select_minimal_probe_candidate(
        master,
        special,
        mapping,
        previously_tested=PREVIOUSLY_TESTED_PRODUCTS,
    )
    mapped = map_product_to_shijiu(
        product,
        TARGET_CATEGORY,
        excluded_product_numbers=special,
    )
    if not mapped.get("publish_ready") or len(mapped.get("source_variants") or []) != 1:
        raise LiveImportError("selected canonical validation item is not a publishable single-variant product")
    if mapped["product_number"] in PREVIOUSLY_TESTED_PRODUCTS:
        raise LiveImportError("candidate was already used by a previous create attempt")
    return mapped, special, selection


def new_checkpoint(item: dict[str, Any], selection: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "mode": "SINGLE_CANONICAL_CREATE_VALIDATION",
        "created_at": now(),
        "updated_at": now(),
        "status": "READY",
        "scope": {
            "product_numbers": [item["product_number"]],
            "maximum_create_requests": 1,
            "legacy_reference_touched": False,
            "legacy_cleanup_executed": False,
            "pdf_special_exclusion_count": EXPECTED_SPECIAL_COUNT,
        },
        "selection": selection,
        "canonical_browser_evidence": evidence,
        "source_payload_sha256": item["payload_sha256"],
        "image_uploads": {},
        "create_attempts": 0,
        "create_intent_at": None,
        "create_response": None,
        "shijiu_product_id": None,
        "readback": None,
        "mapping_persisted": False,
        "request_ledger": [],
        "error": None,
    }


class CanonicalCreateRunner:
    def __init__(
        self,
        client: ShijiuLiveClient,
        item: dict[str, Any],
        special: set[str],
        selection: dict[str, Any],
        browser_evidence: dict[str, Any],
        checkpoint_path: Path,
        mapping_path: Path,
        report_path: Path,
        *,
        confirmation: str,
    ) -> None:
        self.client = client
        self.item = item
        self.special = special
        self.checkpoint_path = checkpoint_path
        self.mapping_path = mapping_path
        self.report_path = report_path
        self.confirmation = confirmation
        if checkpoint_path.exists():
            self.checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        else:
            self.checkpoint = new_checkpoint(item, selection, browser_evidence)
            self._persist()
        if self.checkpoint.get("scope", {}).get("product_numbers") != [item["product_number"]]:
            raise LiveImportError("canonical validation checkpoint identity drift")
        self._request_cursor = 0

    def _report(self) -> dict[str, Any]:
        ledger = self.checkpoint.get("request_ledger") or []
        readback = self.checkpoint.get("readback") or {}
        variants = readback.get("skus") or []
        create_response = self.checkpoint.get("create_response") or {}
        create_data = create_response.get("data")
        return {
            "schema_version": 1,
            "generated_at": now(),
            "mode": "SINGLE_CANONICAL_CREATE_VALIDATION",
            "source": SOURCE_CODE,
            "target": "SHIJIU",
            "status": self.checkpoint.get("status"),
            "product_number": self.item["product_number"],
            "source_product_id": self.item["source_product_id"],
            "shijiu_product_id": self.checkpoint.get("shijiu_product_id"),
            "target_category_id": 294884,
            "default_visibility": "OFF_SHELF_IS_SHELF_0",
            "canonical_contract_verified_before_write": True,
            "create_request_budget": 1,
            "create_attempts": self.checkpoint.get("create_attempts", 0),
            "create_request_count": sum(row.get("path") == "/shopapi/Goods/newAddGood" for row in ledger),
            "image_upload_count": sum(row.get("path") == "/v1/cos/upload" for row in ledger),
            "read_request_count": sum(row.get("semantic_operation") == "read" for row in ledger),
            "mapping_persisted": self.checkpoint.get("mapping_persisted", False),
            "create_response": ({
                "code": create_response.get("code"),
                "msg": create_response.get("msg"),
                "data_shape": "empty_list" if isinstance(create_data, list) and not create_data else type(create_data).__name__,
                "product_id_exposed": bool(_product_id_from_value(create_response)),
            } if create_response else None),
            "exact_backend_sku_codes": [
                row["backend_sku_code"] for row in self.item["source_variants"]
            ],
            "exact_backend_sku_match_count": (
                1 if readback.get("passed") else 0
            ),
            "variant_identity_policy": "shijiu_product_id + exact backend_sku_code",
            "shijiu_sku_id_policy": "nullable; never guessed",
            "verified_variants": variants,
            "price_source": "mini_program_price_jpy",
            "currency": "JPY",
            "currency_conversion_applied": False,
            "pdf_special_exclusion_count": len(self.special),
            "pdf_special_product_selected": self.item["product_number"] in self.special,
            "legacy_reference_touched": False,
            "legacy_cleanup_executed": False,
            "batch_continuation": "STOP_AFTER_THIS_PRODUCT",
            "additional_product_create_requests_allowed": False,
            "error": self.checkpoint.get("error"),
            "sensitive_values_included": False,
        }

    def _persist(self) -> None:
        cursor = getattr(self, "_request_cursor", 0)
        self.checkpoint.setdefault("request_ledger", []).extend(
            copy.deepcopy(self.client.requests[cursor:])
        )
        self._request_cursor = len(self.client.requests)
        self.checkpoint["updated_at"] = now()
        write_json_atomic(self.checkpoint_path, self.checkpoint)
        write_json_atomic(self.report_path, self._report())

    def _stop(self, error: Exception) -> None:
        self.checkpoint["status"] = "STOPPED_ON_FIRST_ERROR"
        self.checkpoint["error"] = {
            "type": type(error).__name__,
            "message": str(error),
            "at": now(),
        }
        self._persist()

    def _complete_post_create_readback(self, response: dict[str, Any]) -> dict[str, Any]:
        if self.checkpoint.get("create_attempts") != 1:
            raise DuplicateRiskError("post-create resume requires exactly one recorded create attempt")
        payload = _resolve_payload(self.item, self.checkpoint["image_uploads"])
        validate_canonical_create_payload(payload)
        if content_sha256(payload) != self.checkpoint.get("resolved_payload_sha256"):
            raise DuplicateRiskError("post-create resume payload hash drift")
        backend_code = self.item["source_variants"][0]["backend_sku_code"]
        matches: list[dict[str, Any]] = []
        for delay in (0, 2, 5, 10):
            if delay:
                time.sleep(delay)
            matches = _unique_exact_product_matches(self.client, backend_code)
            if matches:
                break
        if len(matches) != 1:
            raise ContractMismatchError(
                f"exact backend SKU readback returned {len(matches)} product matches"
            )
        list_row = matches[0]
        product_id = str(
            list_row.get("id") or list_row.get("good_id") or list_row.get("goods_id") or ""
        )
        response_id = _product_id_from_value(response)
        if not product_id or (response_id and str(response_id) != product_id):
            raise ContractMismatchError("create response and Goods.index product identity mismatch")
        self.checkpoint["shijiu_product_id"] = product_id
        self._persist()
        detail = self.client.product_detail(product_id)
        readback = validate_product_readback(
            self.item,
            payload,
            product_id,
            detail,
            create_response=response,
            list_row=list_row,
        )
        persist_verified_mapping(
            self.mapping_path,
            self.item,
            readback,
            content_sha256(payload),
        )
        self.checkpoint.update({
            "status": "COMPLETED",
            "readback": readback,
            "mapping_persisted": True,
            "completed_at": now(),
            "error": None,
        })
        self._persist()
        return self._report()

    def run(self) -> dict[str, Any]:
        if self.confirmation != CANONICAL_CREATE_CONFIRMATION:
            raise LiveImportError("exact single-create confirmation phrase missing")
        if self.checkpoint.get("status") == "COMPLETED":
            self._persist()
            return self._report()
        if self.checkpoint.get("status") == "CREATE_RESPONSE_RECEIVED":
            try:
                return self._complete_post_create_readback(
                    self.checkpoint.get("create_response") or {}
                )
            except Exception as error:
                self._stop(error)
                raise
        if self.checkpoint.get("status") != "READY":
            raise LiveImportError("canonical create checkpoint is terminal; retry is forbidden")
        try:
            if len(self.special) != EXPECTED_SPECIAL_COUNT or self.item["product_number"] in self.special:
                raise LiveImportError(f"{PDF_SPECIAL_EXCLUDED_REASON}: candidate rejected before target access")
            if self.item["product_number"] in PREVIOUSLY_TESTED_PRODUCTS:
                raise LiveImportError("previously tested product cannot enter canonical validation")
            mapping = load_mapping_state(self.mapping_path)
            mapping_row = mapping["products"][self.item["product_number"]]
            if mapping_row.get("shijiu_product_id") not in (None, ""):
                raise DuplicateRiskError("candidate already has a persisted Shijiu mapping")
            validate_live_mikihouse_category(TARGET_CATEGORY, self.client.categories())
            backend_code = self.item["source_variants"][0]["backend_sku_code"]
            if _unique_exact_product_matches(self.client, backend_code):
                raise DuplicateRiskError("exact backend SKU already exists before create")
            self.checkpoint["preflight"] = {
                "passed": True,
                "fixed_category_id": 294884,
                "exact_backend_sku_absent": True,
                "legacy_reference_scanned_or_bound": False,
            }
            self._persist()
            for image in self.item["image_upload_plan"]:
                reference = image["upload_reference"]
                self.checkpoint["image_uploads"][reference] = {
                    "status": "UPLOAD_INTENT_PERSISTED",
                    "source_url": image["source_url"],
                    "source_url_sha256": hashlib.sha256(image["source_url"].encode()).hexdigest(),
                    "order": image["order"],
                    "role": image["role"],
                }
                self._persist()
                target_url, response = self.client.upload_image(
                    image["source_url"], confirmation=self.confirmation
                )
                self.checkpoint["image_uploads"][reference].update({
                    "status": "UPLOADED",
                    "target_url": target_url,
                    "response": _redacted_response(response),
                })
                self._persist()
            payload = _resolve_payload(self.item, self.checkpoint["image_uploads"])
            validate_canonical_create_payload(payload)
            self.checkpoint.update({
                "status": "CREATE_INTENT_PERSISTED",
                "create_intent_at": now(),
                "resolved_payload_sha256": content_sha256(payload),
            })
            self._persist()
            self.checkpoint["create_attempts"] += 1
            response = self.client.create_product(payload, confirmation=self.confirmation)
            self.checkpoint["create_response"] = _redacted_response(response)
            self.checkpoint["status"] = "CREATE_RESPONSE_RECEIVED"
            self._persist()
            return self._complete_post_create_readback(response)
        except Exception as error:
            self._stop(error)
            raise
