from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

from .csv_input import read_product_numbers
from .shijiu_canonical_create import load_verified_browser_credentials
from .shijiu_import import (
    EXPECTED_SPECIAL_COUNT,
    PDF_SPECIAL_EXCLUDED_REASON,
    SOURCE_CODE,
    content_sha256,
    _classification_name,
    load_mapping_state,
    map_product_to_shijiu,
    now,
    recursively_find_skus,
    validate_live_mikihouse_category,
    write_json_atomic,
)
from .shijiu_duplicate_name_identity import (
    UNIQUE_STRONG_MATCH,
    resolve_duplicate_good_name_candidates,
)
from .shijiu_live_import import (
    CREATE_PATH,
    DETAIL_PATH,
    IMAGE_UPLOAD_PATH,
    LIST_PATH,
    ContractMismatchError,
    DuplicateRiskError,
    LiveImportError,
    ShijiuLiveClient,
    UI_READ_INITIAL_BACKOFF_SECONDS,
    UI_READ_MAX_RETRIES,
    UI_TRANSIENT_HTTP_STATUS_CODES,
    _parse_json_response,
    _redacted_response,
    _resolve_payload,
    is_transient_ui_read_error as _is_transient_ui_read_error,
    persist_verified_mapping,
    response_rows,
    validate_canonical_create_payload,
    validate_product_readback,
)


COMPLEX_WRITE_CONFIRMATION = "MIKIHOUSE_COMPLEX_5_REAL_IMPORT"
COMPLEX_BATCH_SIZE = 5
TARGET_CATEGORY_ID = 294884
PREVIOUSLY_TESTED_PRODUCTS = {"00-1000-028", "17-1366-244", "36-2001-572"}
UI_MODE = "MIKIHOUSE_COMPLEX_BATCH_UI_CONTEXT_READBACK"
UI_ALLOWED_PATHS = {LIST_PATH, DETAIL_PATH}


class UiStrongReadbackError(ContractMismatchError):
    def __init__(self, message: str, evidence: dict[str, Any]) -> None:
        super().__init__(message)
        self.evidence = evidence


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _row_id(row: dict[str, Any]) -> str:
    return str(row.get("id") or row.get("good_id") or row.get("goods_id") or "").strip()


def _row_name(row: dict[str, Any]) -> str:
    return str(row.get("good_name") or row.get("goods_name") or row.get("name") or "").strip()


def _response_count(response: dict[str, Any]) -> int | None:
    for container in (response, response.get("data")):
        if not isinstance(container, dict):
            continue
        value = container.get("count")
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _latest_private_file(private_dir: Path, pattern: str) -> Path:
    matches = sorted(private_dir.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    if not matches:
        raise LiveImportError(f"missing Git-external private evidence: {pattern}")
    return matches[0]


class UiContextReadClient:
    """Read-only client reconstructed from the proven browser Goods.index request.

    Authentication values remain in memory. Request evidence exposed to callers is
    limited to paths, counts, hashes and non-sensitive filter values.
    """

    def __init__(self, private_dir: Path, canonical_contract_path: Path, *, timeout: float = 90) -> None:
        contract = json.loads(canonical_contract_path.read_text(encoding="utf-8"))
        canonical_hash = str(contract.get("browser_exact_private_evidence_sha256") or "")
        ui_path = _latest_private_file(
            private_dir, "shijiu-ui-context-reconciliation-*.private.json"
        )
        raw_bytes = ui_path.read_bytes()
        raw = json.loads(raw_bytes)
        if raw.get("browser_create_capture_sha256") != canonical_hash:
            raise LiveImportError("UI-context evidence and canonical CREATE evidence differ")
        safety = raw.get("safety") or {}
        if safety.get("target_mutation_requests_sent") != 0:
            raise LiveImportError("UI-context source evidence contains a target mutation")
        request = raw.get("ui_goods_index_request") or {}
        if request.get("method") != "POST" or LIST_PATH not in str(request.get("url") or ""):
            raise LiveImportError("UI-context source evidence lacks the real Goods.index request")
        self.url = str(request["url"])
        self.headers = {
            str(key): str(value)
            for key, value in (request.get("headers") or {}).items()
            if str(key).lower() not in {"host", "content-length"}
        }
        self.base_pairs = urllib.parse.parse_qsl(
            str(request.get("post_data") or ""), keep_blank_values=True
        )
        self.base_form = dict(self.base_pairs)
        parsed = urllib.parse.urlparse(self.url.replace("&token=", "?token=", 1))
        self.query_token = (urllib.parse.parse_qs(parsed.query).get("token") or [""])[0]
        if not self.query_token or not self.base_form.get("secret"):
            raise LiveImportError("UI-context request authentication is incomplete")
        self.timeout = timeout
        self.requests: list[dict[str, Any]] = []
        self.evidence_sha256 = _sha256_bytes(raw_bytes)
        self.canonical_create_evidence_sha256 = canonical_hash

    def _record(self, path: str, operation: str, metadata: dict[str, Any]) -> dict[str, Any]:
        record = {
            "sequence": len(self.requests) + 1,
            "at": now(),
            "method": "POST",
            "path": path,
            "semantic_operation": "read",
            "operation": operation,
            **metadata,
        }
        self.requests.append(record)
        return record

    def _post(self, url: str, pairs: list[tuple[str, str]], *, path: str, operation: str) -> dict[str, Any]:
        if path not in UI_ALLOWED_PATHS or CREATE_PATH in url or IMAGE_UPLOAD_PATH in url:
            raise LiveImportError("UI-context client blocked a non-read endpoint")
        body = urllib.parse.urlencode(pairs).encode("utf-8")
        result: dict[str, Any] | None = None
        for retry_index in range(UI_READ_MAX_RETRIES + 1):
            record = self._record(path, operation, {
                "body_sha256": _sha256_bytes(body),
                "attempt": retry_index + 1,
                "retry_index": retry_index,
                "maximum_read_retries": UI_READ_MAX_RETRIES,
            })
            request = urllib.request.Request(url, data=body, method="POST", headers=self.headers)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    result = _parse_json_response(response, response.read(), operation)
            except Exception as error:
                transient = _is_transient_ui_read_error(error)
                record.update({
                    "outcome": "TRANSIENT_READ_ERROR" if transient else "NON_RETRYABLE_ERROR",
                    "error_type": type(error).__name__,
                    "http_status": error.code if isinstance(error, urllib.error.HTTPError) else None,
                    "retry_scheduled": transient and retry_index < UI_READ_MAX_RETRIES,
                })
                if not transient or retry_index >= UI_READ_MAX_RETRIES:
                    raise
                time.sleep(UI_READ_INITIAL_BACKOFF_SECONDS * (2 ** retry_index))
                continue
            record.update({
                "outcome": "SUCCESS",
                "retry_scheduled": False,
            })
            break
        if result is None:
            raise LiveImportError("UI-context read retry loop ended without a result")
        if str(result.get("code")) not in {"1", "200"}:
            raise ContractMismatchError(
                f"{operation} failed: code={result.get('code')!r}, msg={result.get('msg')!r}"
            )
        return result

    def _query_pairs(self, good_name: str, good_type: str, page: int) -> list[tuple[str, str]]:
        replacements = {"good_name": good_name, "good_type": good_type, "page": str(page)}
        pairs: list[tuple[str, str]] = []
        seen: set[str] = set()
        for key, value in self.base_pairs:
            pairs.append((key, replacements.get(key, value)))
            seen.add(key)
        for key in ("good_name", "good_type", "page"):
            if key not in seen:
                pairs.append((key, replacements[key]))
        return pairs

    def exact_name_candidates(self, good_name: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        expected = str(good_name).strip()
        if not expected:
            raise LiveImportError("UI-context readback requires an exact non-empty good_name")
        matches: dict[str, dict[str, Any]] = {}
        summaries = []
        for label, good_type in (("category_294884", str(TARGET_CATEGORY_ID)), ("all_categories", "")):
            first_pairs = self._query_pairs(expected, good_type, 1)
            first = self._post(
                self.url, first_pairs, path=LIST_PATH, operation=f"UI-context {label} exact-name query"
            )
            page_size = max(1, int(dict(first_pairs).get("page_size") or 20))
            declared = _response_count(first)
            if declared is None:
                raise LiveImportError(
                    "UI-context exact-name query lacks a declared count; candidate set is incomplete"
                )
            page_count = max(1, math.ceil(declared / page_size))
            if page_count > 100:
                raise LiveImportError(
                    "UI-context exact-name candidate set exceeds the 100-page safety ceiling"
                )
            rows = response_rows(first)
            for page in range(2, page_count + 1):
                rows.extend(response_rows(self._post(
                    self.url,
                    self._query_pairs(expected, good_type, page),
                    path=LIST_PATH,
                    operation=f"UI-context {label} exact-name query page {page}",
                )))
            exact_rows = [row for row in rows if _row_name(row) == expected and _row_id(row)]
            for row in exact_rows:
                matches[_row_id(row)] = row
            summaries.append({
                "label": label,
                "good_type": good_type,
                "declared_count": declared,
                "page_size": page_size,
                "pages_read": page_count,
                "all_declared_pages_read": True,
                "exact_match_product_ids": sorted({_row_id(row) for row in exact_rows}),
            })
        return list(matches.values()), {
            "primary_identity_path": (
                "browser-captured UI-context Goods.index exact good_name -> product_id -> "
                "getFormatInfo exact backend sku_code"
            ),
            "exact_good_name": expected,
            "candidate_product_ids": sorted(matches),
            "queries": summaries,
            "filter_context": {
                key: value
                for key, value in self.base_form.items()
                if key not in {"token", "secret"}
            },
            "auth_values_included": False,
        }

    def list_context_rows(
        self,
        *,
        good_type: str = "",
        max_pages: int = 100,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Read every row visible through the captured UI filter context.

        Only the product-name filter is cleared and the category/page fields are
        varied. All other browser-captured form fields and their order are kept.
        The returned summary intentionally excludes authentication values.
        """
        if max_pages < 1:
            raise LiveImportError("UI-context max_pages must be positive")
        first_pairs = self._query_pairs("", str(good_type), 1)
        first = self._post(
            self.url,
            first_pairs,
            path=LIST_PATH,
            operation="UI-context capacity-audit list page 1",
        )
        page_size = max(1, int(dict(first_pairs).get("page_size") or 20))
        declared = _response_count(first)
        required_pages = max(1, math.ceil((declared or 0) / page_size))
        if required_pages > max_pages:
            raise LiveImportError(
                f"UI-context capacity audit requires {required_pages} pages; "
                f"configured maximum is {max_pages}"
            )
        rows = response_rows(first)
        for page in range(2, required_pages + 1):
            rows.extend(response_rows(self._post(
                self.url,
                self._query_pairs("", str(good_type), page),
                path=LIST_PATH,
                operation=f"UI-context capacity-audit list page {page}",
            )))
        unique: dict[str, dict[str, Any]] = {}
        for row in rows:
            product_id = _row_id(row)
            if product_id:
                unique[product_id] = row
        if declared is not None and len(unique) != declared:
            raise ContractMismatchError(
                "UI-context capacity audit did not enumerate the declared unique product count: "
                f"declared={declared}, unique={len(unique)}"
            )
        return list(unique.values()), {
            "good_type": str(good_type),
            "declared_count": declared,
            "unique_product_count": len(unique),
            "page_size": page_size,
            "pages_read": required_pages,
            "all_declared_rows_enumerated": declared is not None and len(unique) == declared,
            "preserved_filter_context": {
                key: value
                for key, value in self.base_form.items()
                if key not in {"token", "secret", "good_name", "good_type", "page"}
            },
            "auth_values_included": False,
        }

    def sample_context_rows(
        self,
        *,
        good_type: str = "",
        sample_page_count: int = 32,
        max_declared_pages: int = 500,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Deterministically sample evenly spaced pages from the captured UI context."""
        if sample_page_count < 1 or max_declared_pages < 1:
            raise LiveImportError("UI-context sampling bounds must be positive")
        first_pairs = self._query_pairs("", str(good_type), 1)
        first = self._post(
            self.url,
            first_pairs,
            path=LIST_PATH,
            operation="UI-context capacity-audit sampled list page 1",
        )
        page_size = max(1, int(dict(first_pairs).get("page_size") or 20))
        declared = _response_count(first)
        declared_pages = max(1, math.ceil((declared or 0) / page_size))
        if declared_pages > max_declared_pages:
            raise LiveImportError(
                f"UI-context declared page count {declared_pages} exceeds safety ceiling "
                f"{max_declared_pages}"
            )
        count = min(sample_page_count, declared_pages)
        if count == 1:
            sampled_pages = [1]
        else:
            sampled_pages = sorted({
                1 + round(index * (declared_pages - 1) / (count - 1))
                for index in range(count)
            })
        rows = response_rows(first)
        for page in sampled_pages:
            if page == 1:
                continue
            rows.extend(response_rows(self._post(
                self.url,
                self._query_pairs("", str(good_type), page),
                path=LIST_PATH,
                operation=f"UI-context capacity-audit sampled list page {page}",
            )))
        unique: dict[str, dict[str, Any]] = {}
        for row in rows:
            product_id = _row_id(row)
            if product_id:
                unique[product_id] = row
        return list(unique.values()), {
            "good_type": str(good_type),
            "declared_count": declared,
            "declared_page_count": declared_pages,
            "page_size": page_size,
            "sampled_page_count": len(sampled_pages),
            "sampled_pages": sampled_pages,
            "sampled_unique_product_count": len(unique),
            "sampling_policy": "deterministic_evenly_spaced_pages_including_first_and_last",
            "sampling_is_deterministic": True,
            "all_declared_rows_enumerated": len(sampled_pages) == declared_pages,
            "preserved_filter_context": {
                key: value
                for key, value in self.base_form.items()
                if key not in {"token", "secret", "good_name", "good_type", "page"}
            },
            "auth_values_included": False,
        }

    def product_detail(self, product_id: str) -> dict[str, Any]:
        prefix = self.url.split("/shopapi/", 1)[0]
        detail_url = f"{prefix}{DETAIL_PATH}&token={urllib.parse.quote(self.query_token)}"
        pairs = [("secret", self.base_form["secret"])]
        if self.base_form.get("token"):
            pairs.append(("token", self.base_form["token"]))
        pairs.append(("id", str(product_id)))
        return self._post(
            detail_url,
            pairs,
            path=DETAIL_PATH,
            operation="UI-context getFormatInfo strong readback",
        )

    def safe_contract_summary(self) -> dict[str, Any]:
        parsed = urllib.parse.urlparse(self.url.replace("&token=", "?token=", 1))
        return {
            "method": "POST",
            "endpoint": {"scheme": parsed.scheme, "host": parsed.netloc, "path": parsed.path},
            "header_names": sorted(key.lower() for key in self.headers),
            "form_field_names_in_order": [key for key, _ in self.base_pairs],
            "filter_context": {
                key: value
                for key, value in self.base_form.items()
                if key not in {"token", "secret"}
            },
            "query_token_present": True,
            "body_secret_present": True,
            "credential_values_persisted": False,
            "private_evidence_sha256": self.evidence_sha256,
            "canonical_create_evidence_sha256": self.canonical_create_evidence_sha256,
        }


def _metrics(product: dict[str, Any]) -> dict[str, Any]:
    variants = list(product.get("variants") or [])
    colors = {str(row.get("color") or "") for row in variants if row.get("color")}
    sizes = {str(row.get("size") or "") for row in variants if row.get("size")}
    images = list(product.get("ordered_images") or [])
    return {
        "variant_count": len(variants),
        "available_variant_count": sum(bool(row.get("available_for_sale")) for row in variants),
        "color_count": len(colors),
        "size_count": len(sizes),
        "image_count": len(images),
        "gallery_or_detail_image_count": sum(
            str(row.get("role") or "") in {"product_gallery", "detail"} for row in images
        ),
    }


def _candidate_pool(
    master: dict[str, Any], special: set[str], mapping: dict[str, Any], target_category: dict[str, Any]
) -> list[dict[str, Any]]:
    names = Counter(str(product.get("name") or "").strip() for product in master.get("products") or [])
    pool = []
    for product in master.get("products") or []:
        number = str(product.get("product_number") or "")
        if (
            not number
            or number in special
            or number in PREVIOUSLY_TESTED_PRODUCTS
            or not product.get("active")
            or (mapping.get("products", {}).get(number) or {}).get("shijiu_product_id") not in (None, "")
        ):
            continue
        metrics = _metrics(product)
        if metrics["variant_count"] < 2 or metrics["available_variant_count"] < 1:
            continue
        mapped = map_product_to_shijiu(product, target_category, excluded_product_numbers=special)
        if not mapped.get("publish_ready"):
            continue
        pool.append({
            "product": product,
            "mapped": mapped,
            "classification": _classification_name(product),
            "name_unique_in_source": names[str(product.get("name") or "").strip()] == 1,
            **metrics,
        })
    return pool


def _distance(row: dict[str, Any], variants: int, images: int) -> tuple[Any, ...]:
    return (
        0 if row["name_unique_in_source"] else 1,
        abs(row["variant_count"] - variants),
        abs(row["image_count"] - images),
        str(row["product"]["product_number"]),
    )


def select_complex_batch(
    master: dict[str, Any], special: set[str], mapping: dict[str, Any], target_category: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if len(special) != EXPECTED_SPECIAL_COUNT:
        raise LiveImportError(f"expected {EXPECTED_SPECIAL_COUNT} permanent PDF special exclusions")
    pool = _candidate_pool(master, special, mapping, target_category)
    selected: list[tuple[str, dict[str, Any]]] = []
    used: set[str] = set()

    role_specs = [
        ("multi_color_multi_size_footwear", lambda row: (
            row["classification"] == "footwear" and 10 <= row["variant_count"] <= 40
            and row["color_count"] >= 2 and row["size_count"] >= 2 and row["image_count"] <= 45
        ), 30, 30),
        ("high_sku_apparel", lambda row: (
            row["classification"] == "apparel" and 10 <= row["variant_count"] <= 24
            and row["color_count"] >= 2 and row["size_count"] >= 2 and 20 <= row["image_count"] <= 75
        ), 18, 60),
        ("rich_gallery_and_details", lambda row: (
            row["classification"] in {"goods", "other"} and 3 <= row["variant_count"] <= 10
            and 40 <= row["image_count"] <= 80
        ), 6, 70),
        ("baby_product", lambda row: (
            row["classification"] == "baby" and 2 <= row["variant_count"] <= 8
            and 15 <= row["image_count"] <= 45
        ), 3, 30),
        ("ordinary_goods", lambda row: (
            row["classification"] == "goods" and 2 <= row["variant_count"] <= 6
            and 10 <= row["image_count"] <= 30
            and "ゴールド" not in str(row["product"].get("name") or "")
        ), 3, 20),
    ]
    for role, predicate, target_variants, target_images in role_specs:
        eligible = [
            row for row in pool
            if row["product"]["product_number"] not in used and predicate(row)
        ]
        if not eligible:
            raise LiveImportError(f"no eligible complex candidate for role {role}")
        chosen = min(eligible, key=lambda row: _distance(row, target_variants, target_images))
        number = chosen["product"]["product_number"]
        used.add(number)
        selected.append((role, chosen))

    items = [copy.deepcopy(row["mapped"]) for _, row in selected]
    selection = {
        "schema_version": 1,
        "generated_at": now(),
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "selection_policy": "deterministic bounded-complexity representative roles",
        "fixed_target_category_id": TARGET_CATEGORY_ID,
        "pdf_special_exclusion_count": len(special),
        "previously_tested_excluded": sorted(PREVIOUSLY_TESTED_PRODUCTS),
        "eligible_pool_count": len(pool),
        "products": [
            {
                "role": role,
                "product_number": row["product"]["product_number"],
                "good_name": row["mapped"]["shijiu_payload_preview"]["good_name"],
                "classification": row["classification"],
                "name_unique_in_source": row["name_unique_in_source"],
                "variant_count": row["variant_count"],
                "available_variant_count": row["available_variant_count"],
                "color_count": row["color_count"],
                "size_count": row["size_count"],
                "image_count": row["image_count"],
                "gallery_or_detail_image_count": row["gallery_or_detail_image_count"],
                "payload_sha256": row["mapped"]["payload_sha256"],
            }
            for role, row in selected
        ],
    }
    if len(items) != COMPLEX_BATCH_SIZE or set(used) & special:
        raise LiveImportError("complex batch selection boundary failed")
    return items, selection


def initial_checkpoint(
    items: list[dict[str, Any]],
    selection: dict[str, Any],
    *,
    mode: str = "COMPLEX_5_REAL_IMPORT_VALIDATION",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "mode": mode,
        "created_at": now(),
        "updated_at": now(),
        "status": "READY",
        "fixed_target_category_id": TARGET_CATEGORY_ID,
        "selection_sha256": content_sha256(selection["products"]),
        "legacy_reference_touched": False,
        "legacy_cleanup_executed": False,
        "records": {
            item["product_number"]: {
                "product_number": item["product_number"],
                "source_product_id": item["source_product_id"],
                "payload_sha256": item["payload_sha256"],
                "state": "PLANNED",
                "precreate_ui_absence": None,
                "image_uploads": {},
                "create_attempts": 0,
                "create_response": None,
                "shijiu_product_id": None,
                "readback": None,
                "mapping_persisted": False,
                "error": None,
            }
            for item in items
        },
        "request_ledger": [],
        "stop_reason": None,
    }


def _normalize_ui_detail(detail: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(detail)
    if str(normalized.get("code")) == "200" and str(normalized.get("msg") or "").casefold() == "success":
        normalized["code"] = 1
    return normalized


def ui_precreate_absence(
    ui: UiContextReadClient, item: dict[str, Any]
) -> dict[str, Any]:
    payload = item["shijiu_payload_preview"]
    rows, evidence = ui.exact_name_candidates(payload["good_name"])
    expected = {row["backend_sku_code"] for row in item["source_variants"]}
    observations = []
    collisions: set[str] = set()
    for row in rows:
        product_id = _row_id(row)
        detail = ui.product_detail(product_id)
        codes = {
            str(sku.get("sku_code") or "").strip()
            for sku in recursively_find_skus(detail)
            if str(sku.get("sku_code") or "").strip()
        }
        overlap = sorted(expected & codes)
        collisions.update(overlap)
        observations.append({
            "product_id": product_id,
            "exact_name": True,
            "backend_sku_code_count": len(codes),
            "expected_backend_sku_overlap": overlap,
        })
    if collisions:
        raise DuplicateRiskError(
            f"UI-context preflight found existing exact backend SKU(s): {sorted(collisions)}"
        )
    return {
        "passed": True,
        "identity_path": evidence["primary_identity_path"],
        "candidate_product_ids": evidence["candidate_product_ids"],
        "candidate_observations": observations,
        "expected_backend_sku_count": len(expected),
        "expected_backend_sku_overlap": [],
        "good_code_used_as_primary": False,
        "ui_queries": evidence["queries"],
    }


def ui_strong_readback(
    ui: UiContextReadClient,
    item: dict[str, Any],
    payload: dict[str, Any],
    create_response: dict[str, Any],
    *,
    require_exact_good_details: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    rows, evidence = ui.exact_name_candidates(payload["good_name"])
    detail_by_id = {
        _row_id(row): _normalize_ui_detail(ui.product_detail(_row_id(row)))
        for row in rows if _row_id(row)
    }
    identity = resolve_duplicate_good_name_candidates(
        good_name=payload["good_name"],
        sku_info=payload["sku_info"],
        candidate_rows=rows,
        detail_by_product_id=detail_by_id,
        category_id=TARGET_CATEGORY_ID,
    )
    discovery = {
        **evidence,
        "identity_resolution": identity,
        "verified_product_ids": (
            [identity["shijiu_product_id"]]
            if identity["status"] == UNIQUE_STRONG_MATCH else []
        ),
        "good_code_role": "not_used_for_primary_or_binding",
    }
    if identity["status"] != UNIQUE_STRONG_MATCH:
        raise UiStrongReadbackError(
            "UI-context exact good_name candidate set -> getFormatInfo complete SKU-set "
            f"resolver returned {identity['status']}",
            discovery,
        )
    product_id = str(identity["shijiu_product_id"])
    list_row = next(row for row in rows if _row_id(row) == product_id)
    try:
        readback = validate_product_readback(
            item,
            payload,
            product_id,
            detail_by_id[product_id],
            create_response=create_response,
            list_row=list_row,
            require_is_shelf=False,
            require_exact_good_details=require_exact_good_details,
        )
    except ContractMismatchError as error:
        discovery["full_payload_readback"] = {
            "passed": False,
            "mismatch": str(error),
        }
        raise UiStrongReadbackError(
            "unique strong SKU identity candidate failed full payload readback",
            discovery,
        ) from error
    for sku in readback["skus"]:
        sku["shijiu_sku_id"] = None
    discovery["full_payload_readback"] = {
        "passed": True,
        "verified_backend_sku_count": readback["sku_count"],
        "prices_verified": True,
        "stocks_verified": True,
        "specifications_verified": True,
        "images_verified": True,
    }
    return readback, discovery


def build_next_20_plan(
    master: dict[str, Any], special: set[str], mapping: dict[str, Any], target_category: dict[str, Any]
) -> dict[str, Any]:
    pool = _candidate_pool(master, special, mapping, target_category)
    rows = []
    for classification in ("footwear", "apparel", "baby", "goods"):
        eligible = [
            row for row in pool
            if row["classification"] == classification and row["image_count"] <= 45
        ]
        eligible.sort(key=lambda row: (
            0 if row["name_unique_in_source"] else 1,
            -row["variant_count"],
            row["image_count"],
            row["product"]["product_number"],
        ))
        if len(eligible) < 5:
            raise LiveImportError(f"insufficient next-stage {classification} candidates")
        for row in eligible[:5]:
            rows.append({
                "sequence": len(rows) + 1,
                "classification": classification,
                "product_number": row["product"]["product_number"],
                "good_name": row["mapped"]["shijiu_payload_preview"]["good_name"],
                "variant_count": row["variant_count"],
                "image_count": row["image_count"],
                "payload_sha256": row["mapped"]["payload_sha256"],
                "target_category_id": TARGET_CATEGORY_ID,
                "currency": "JPY",
                "execution_authorized": False,
            })
    return {
        "schema_version": 1,
        "generated_at": now(),
        "status": "PLANNED_NOT_EXECUTED",
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "product_count": len(rows),
        "products": rows,
        "real_write_requests": 0,
        "requires_separate_authorization": True,
        "pdf_special_exclusion_count": len(special),
        "legacy_reference_touched": False,
    }


class ComplexLiveBatchRunner:
    def __init__(
        self,
        client: ShijiuLiveClient,
        ui: UiContextReadClient,
        items: list[dict[str, Any]],
        special: set[str],
        category: dict[str, Any],
        selection: dict[str, Any],
        *,
        checkpoint_path: Path,
        mapping_path: Path,
        report_path: Path,
        readbacks_path: Path,
        confirmation: str,
        expected_batch_size: int = COMPLEX_BATCH_SIZE,
        expected_confirmation: str = COMPLEX_WRITE_CONFIRMATION,
        mode: str = "COMPLEX_5_REAL_IMPORT_VALIDATION",
        prohibited_product_numbers: set[str] | None = None,
    ) -> None:
        self.client = client
        self.ui = ui
        self.items = items
        self.special = special
        self.category = category
        self.selection = selection
        self.checkpoint_path = checkpoint_path
        self.mapping_path = mapping_path
        self.report_path = report_path
        self.readbacks_path = readbacks_path
        self.confirmation = confirmation
        self.expected_batch_size = expected_batch_size
        self.expected_confirmation = expected_confirmation
        self.mode = mode
        self.prohibited_product_numbers = (
            set(PREVIOUSLY_TESTED_PRODUCTS)
            if prohibited_product_numbers is None
            else set(prohibited_product_numbers)
        )
        if checkpoint_path.exists():
            self.checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        else:
            self.checkpoint = initial_checkpoint(items, selection, mode=mode)
            self._persist()
        if self.checkpoint.get("mode") != self.mode:
            raise LiveImportError("complex batch checkpoint mode drift")
        expected = [item["product_number"] for item in items]
        if list((self.checkpoint.get("records") or {}).keys()) != expected:
            raise LiveImportError("complex batch checkpoint identity drift")
        if self.checkpoint.get("selection_sha256") != content_sha256(selection["products"]):
            raise LiveImportError("complex batch selection drift")
        self._client_cursor = 0
        self._ui_cursor = 0

    def _report_documents(self) -> tuple[dict[str, Any], dict[str, Any]]:
        records = self.checkpoint["records"]
        completed = [
            row for row in records.values()
            if row["state"] in {"READBACK_VERIFIED", "READBACK_VERIFIED_AFTER_BATCH_STOP"}
        ]
        ledger = self.checkpoint.get("request_ledger") or []
        report = {
            "schema_version": 1,
            "generated_at": now(),
            "mode": self.mode,
            "status": self.checkpoint.get("status"),
            "source": SOURCE_CODE,
            "target": "SHIJIU",
            "requested_product_count": len(self.items),
            "verified_product_count": len(completed),
            "complex_validation_all_passed": (
                len(completed) == self.expected_batch_size
                and self.checkpoint.get("status") == "COMPLETED"
            ),
            "fixed_target_category_id": TARGET_CATEGORY_ID,
            "price_source": "mini_program_price_jpy=ceil(tax_included_price_jpy*0.65)",
            "currency": "JPY",
            "currency_conversion_applied": False,
            "default_visibility": "state=1,is_shelf=0",
            "selection": self.selection,
            "ui_context_contract": self.ui.safe_contract_summary(),
            "identity_policy": (
                "UI-context Goods.index exact good_name -> unique product_id after full "
                "getFormatInfo verification of every backend sku_code"
            ),
            "variant_identity_policy": "shijiu_product_id + exact backend_sku_code",
            "shijiu_sku_id_policy": "null when no documented official field is available; never guessed",
            "official_source_image_urls_in_formal_payload": False,
            "uploaded_official_image_count": sum(
                image.get("status") == "UPLOADED"
                for row in records.values() for image in row["image_uploads"].values()
            ),
            "verified_sku_count": sum((row.get("readback") or {}).get("sku_count", 0) for row in completed),
            "created_product_ids": [row["shijiu_product_id"] for row in completed],
            "mapping_persisted_product_count": sum(row.get("mapping_persisted", False) for row in completed),
            "request_counts": {
                "read": sum(row.get("semantic_operation") == "read" for row in ledger),
                "write": sum(row.get("semantic_operation") == "write" for row in ledger),
                "image_upload": sum(row.get("path") == IMAGE_UPLOAD_PATH for row in ledger),
                "product_create": sum(row.get("path") == CREATE_PATH for row in ledger),
                "update": 0,
                "legacy_cleanup": 0,
            },
            "fail_closed": True,
            "stop_reason": self.checkpoint.get("stop_reason"),
            "product_results": [
                {
                    "product_number": number,
                    "state": row["state"],
                    "create_attempts": row["create_attempts"],
                    "create_response_summary": ({
                        "code": row["create_response"].get("code"),
                        "msg": row["create_response"].get("msg"),
                        "data_shape": (
                            "empty_list"
                            if isinstance(row["create_response"].get("data"), list)
                            and not row["create_response"].get("data")
                            else type(row["create_response"].get("data")).__name__
                        ),
                    } if isinstance(row.get("create_response"), dict) else None),
                    "shijiu_product_id": row.get("shijiu_product_id"),
                    "uploaded_image_count": sum(
                        image.get("status") == "UPLOADED" for image in row["image_uploads"].values()
                    ),
                    "verified_sku_count": (row.get("readback") or {}).get("sku_count", 0),
                    "mapping_persisted": row.get("mapping_persisted", False),
                    "post_stop_delayed_ui_reconciliation": row.get(
                        "post_stop_delayed_ui_reconciliation"
                    ),
                    "error": row.get("error"),
                }
                for number, row in records.items()
            ],
            "pdf_special_exclusion_count": len(self.special),
            "pdf_special_product_selected": False,
            "legacy_reference_touched": False,
            "legacy_cleanup_executed": False,
            "next_20_executed": False,
            "next_20_plan_generated": False,
            "sensitive_values_included": False,
        }
        readbacks = {
            "schema_version": 1,
            "generated_at": now(),
            "source": SOURCE_CODE,
            "target": "SHIJIU",
            "results": [row["readback"] for row in completed],
            "verified_product_count": len(completed),
            "verified_sku_count": report["verified_sku_count"],
            "all_passed": (
                len(completed) == self.expected_batch_size
                and self.checkpoint.get("status") == "COMPLETED"
            ),
            "sensitive_values_included": False,
        }
        return report, readbacks

    def _persist(self) -> None:
        self.checkpoint.setdefault("request_ledger", []).extend(
            copy.deepcopy(self.client.requests[getattr(self, "_client_cursor", 0):])
        )
        self._client_cursor = len(self.client.requests)
        self.checkpoint["request_ledger"].extend(
            copy.deepcopy(self.ui.requests[getattr(self, "_ui_cursor", 0):])
        )
        self._ui_cursor = len(self.ui.requests)
        self.checkpoint["updated_at"] = now()
        write_json_atomic(self.checkpoint_path, self.checkpoint)
        report, readbacks = self._report_documents()
        write_json_atomic(self.report_path, report)
        write_json_atomic(self.readbacks_path, readbacks)

    def _stop(self, record: dict[str, Any] | None, error: Exception) -> None:
        if record is not None:
            record["error"] = {"type": type(error).__name__, "message": str(error), "at": now()}
            if record["state"] not in {"CREATE_INTENT_PERSISTED", "CREATE_RESULT_UNKNOWN"}:
                record["state"] = "STOPPED_ON_ERROR"
        self.checkpoint["status"] = "STOPPED_ON_FIRST_ERROR"
        self.checkpoint["stop_reason"] = {
            "type": type(error).__name__, "message": str(error), "at": now()
        }
        self._persist()

    def _batch_preflight(self) -> None:
        if len(self.items) != self.expected_batch_size or len(self.special) != EXPECTED_SPECIAL_COUNT:
            raise LiveImportError("complex batch size or permanent exclusion count changed")
        numbers = {item["product_number"] for item in self.items}
        if numbers & self.special:
            raise LiveImportError(f"{PDF_SPECIAL_EXCLUDED_REASON}: complex batch boundary failure")
        if numbers & self.prohibited_product_numbers:
            raise LiveImportError("prohibited or previously attempted product entered the complex batch")
        validate_live_mikihouse_category(self.category, self.client.categories())
        mapping = load_mapping_state(self.mapping_path)
        for item in self.items:
            row = mapping["products"][item["product_number"]]
            checkpoint_row = self.checkpoint["records"][item["product_number"]]
            mapped_id = row.get("shijiu_product_id")
            if checkpoint_row["state"] == "READBACK_VERIFIED":
                if str(mapped_id) != str(checkpoint_row["shijiu_product_id"]):
                    raise DuplicateRiskError("checkpoint/mapping product identity mismatch")
            elif mapped_id not in (None, ""):
                raise DuplicateRiskError(f"candidate became mapped: {item['product_number']}")
        self.checkpoint["batch_preflight"] = {
            "passed": True,
            "at": now(),
            "category_id": TARGET_CATEGORY_ID,
            "unmapped_product_count": sum(
                row["state"] != "READBACK_VERIFIED" for row in self.checkpoint["records"].values()
            ),
            "legacy_reference_scanned_or_bound": False,
        }
        self._persist()

    def _post_create_readback(self, item: dict[str, Any], record: dict[str, Any]) -> None:
        payload = _resolve_payload(item, record["image_uploads"])
        if content_sha256(payload) != record.get("resolved_payload_sha256"):
            raise DuplicateRiskError("resolved payload changed after CREATE")
        try:
            readback, discovery = ui_strong_readback(
                self.ui, item, payload, record.get("create_response") or {}
            )
        except UiStrongReadbackError as error:
            record["ui_readback_discovery"] = error.evidence
            self._persist()
            raise
        record["ui_readback_discovery"] = discovery
        record["shijiu_product_id"] = readback["shijiu_product_id"]
        self._persist()
        persist_verified_mapping(self.mapping_path, item, readback, content_sha256(payload))
        record.update({
            "state": "READBACK_VERIFIED",
            "readback": readback,
            "mapping_persisted": True,
            "verified_at": readback["verified_at"],
            "error": None,
        })
        self._persist()

    def reconcile_stopped_first_create(self) -> dict[str, Any]:
        """Perform only delayed UI reads after a frozen CREATE anomaly.

        This method never invokes upload/create/update and never resumes later
        products. A unique full match may repair only the local mapping.
        """
        if self.checkpoint.get("status") != "STOPPED_ON_FIRST_ERROR":
            raise LiveImportError("post-stop reconciliation requires a frozen batch")
        attempted = [
            (item, self.checkpoint["records"][item["product_number"]])
            for item in self.items
            if self.checkpoint["records"][item["product_number"]].get("create_attempts") == 1
        ]
        if len(attempted) != 1 or any(
            row.get("create_attempts") not in {0, 1}
            for row in self.checkpoint["records"].values()
        ):
            raise DuplicateRiskError("expected exactly one historical CREATE attempt")
        item, record = attempted[0]
        payload = _resolve_payload(item, record["image_uploads"])
        try:
            readback, discovery = ui_strong_readback(
                self.ui, item, payload, record.get("create_response") or {}
            )
        except UiStrongReadbackError as error:
            record["post_stop_delayed_ui_reconciliation"] = {
                "at": now(),
                "status": "NO_UNIQUE_FULL_MATCH",
                **error.evidence,
                "target_mutations": 0,
            }
            self._persist()
            return {
                "status": "BATCH_FROZEN_CREATE_NOT_VERIFIED",
                "product_number": item["product_number"],
                "candidate_product_ids": error.evidence["candidate_product_ids"],
                "verified_product_ids": error.evidence["verified_product_ids"],
                "target_mutations": 0,
            }
        persist_verified_mapping(self.mapping_path, item, readback, content_sha256(payload))
        record.update({
            "state": "READBACK_VERIFIED_AFTER_BATCH_STOP",
            "shijiu_product_id": readback["shijiu_product_id"],
            "readback": readback,
            "mapping_persisted": True,
            "post_stop_delayed_ui_reconciliation": {
                "at": now(),
                "status": "UNIQUE_FULL_MATCH_MAPPING_REPAIRED_BATCH_REMAINS_FROZEN",
                **discovery,
                "target_mutations": 0,
            },
        })
        self._persist()
        return {
            "status": "MAPPING_REPAIRED_BATCH_REMAINS_FROZEN",
            "product_number": item["product_number"],
            "shijiu_product_id": readback["shijiu_product_id"],
            "target_mutations": 0,
        }

    def run(self) -> dict[str, Any]:
        if self.confirmation != self.expected_confirmation:
            raise LiveImportError("exact complex-batch write confirmation missing")
        if self.checkpoint.get("status") == "COMPLETED":
            self._persist()
            return self._report_documents()[0]
        if self.checkpoint.get("status") == "STOPPED_ON_FIRST_ERROR":
            raise LiveImportError("complex batch checkpoint is frozen after its first anomaly")
        try:
            self._batch_preflight()
        except Exception as error:
            self._stop(None, error)
            raise
        for item in self.items:
            record = self.checkpoint["records"][item["product_number"]]
            try:
                if record["state"] == "READBACK_VERIFIED":
                    continue
                if record["state"] == "CREATE_RESPONSE_RECEIVED":
                    self._post_create_readback(item, record)
                    continue
                if record["state"] in {"CREATE_INTENT_PERSISTED", "CREATE_RESULT_UNKNOWN"}:
                    raise DuplicateRiskError("ambiguous CREATE cannot be retried")
                if record["state"] == "PLANNED":
                    record["precreate_ui_absence"] = ui_precreate_absence(self.ui, item)
                    record["state"] = "UI_PREFLIGHT_PASSED"
                    self._persist()
                for upload in item["image_upload_plan"]:
                    reference = upload["upload_reference"]
                    existing = record["image_uploads"].get(reference)
                    if existing and existing.get("status") == "UPLOADED":
                        continue
                    if existing:
                        raise DuplicateRiskError(f"ambiguous image upload cannot be retried: {reference}")
                    record["image_uploads"][reference] = {
                        "upload_reference": reference,
                        "order": upload["order"],
                        "role": upload["role"],
                        "source_url_sha256": hashlib.sha256(upload["source_url"].encode()).hexdigest(),
                        "target_url": None,
                        "status": "UPLOAD_INTENT_PERSISTED",
                        "intent_at": now(),
                    }
                    record["state"] = "UPLOADING_IMAGES"
                    self._persist()
                    try:
                        target_url, response = self.client.upload_image(
                            upload["source_url"], confirmation=self.confirmation
                        )
                    except Exception:
                        record["image_uploads"][reference]["status"] = "UPLOAD_RESULT_UNKNOWN"
                        self._persist()
                        raise
                    record["image_uploads"][reference].update({
                        "target_url": target_url,
                        "status": "UPLOADED",
                        "completed_at": now(),
                        "response": _redacted_response(response),
                    })
                    self._persist()
                record["state"] = "IMAGES_COMPLETE"
                self._persist()
                payload = _resolve_payload(item, record["image_uploads"])
                validate_canonical_create_payload(payload)
                record.update({
                    "state": "CREATE_INTENT_PERSISTED",
                    "create_intent_at": now(),
                    "resolved_payload_sha256": content_sha256(payload),
                    "create_attempts": 1,
                })
                self._persist()
                try:
                    response = self.client.create_product(payload, confirmation=self.confirmation)
                except Exception:
                    record["state"] = "CREATE_RESULT_UNKNOWN"
                    self._persist()
                    raise
                record["create_response"] = _redacted_response(response)
                record["state"] = "CREATE_RESPONSE_RECEIVED"
                self._persist()
                time.sleep(2)
                self._post_create_readback(item, record)
            except Exception as error:
                self._stop(record, error)
                raise
        self.checkpoint["status"] = "COMPLETED"
        self.checkpoint["completed_at"] = now()
        self.checkpoint["stop_reason"] = None
        self._persist()
        return self._report_documents()[0]


def load_complex_inputs(
    master_path: Path,
    special_path: Path,
    mapping_path: Path,
    target_category: dict[str, Any],
) -> tuple[dict[str, Any], set[str], list[dict[str, Any]], dict[str, Any]]:
    master = json.loads(master_path.read_text(encoding="utf-8"))
    special = set(read_product_numbers(special_path))
    mapping = load_mapping_state(mapping_path)
    items, selection = select_complex_batch(master, special, mapping, target_category)
    return master, special, items, selection


def load_frozen_complex_items(
    master: dict[str, Any],
    special: set[str],
    target_category: dict[str, Any],
    selection: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = selection.get("products") or []
    numbers = [str(row.get("product_number") or "") for row in rows]
    if (
        selection.get("source") != SOURCE_CODE
        or selection.get("target") != "SHIJIU"
        or selection.get("fixed_target_category_id") != TARGET_CATEGORY_ID
        or len(numbers) != COMPLEX_BATCH_SIZE
        or len(set(numbers)) != COMPLEX_BATCH_SIZE
        or set(numbers) & special
        or set(numbers) & PREVIOUSLY_TESTED_PRODUCTS
    ):
        raise LiveImportError("invalid frozen complex-batch selection")
    by_number = {
        str(product.get("product_number") or ""): product
        for product in master.get("products") or []
    }
    items = []
    for row in rows:
        number = str(row["product_number"])
        product = by_number.get(number)
        if not product or not product.get("active"):
            raise LiveImportError(f"frozen complex product missing/inactive in master: {number}")
        item = map_product_to_shijiu(product, target_category, excluded_product_numbers=special)
        if not item.get("publish_ready") or item.get("payload_sha256") != row.get("payload_sha256"):
            raise LiveImportError(f"frozen complex product payload drift: {number}")
        items.append(item)
    return items


def make_live_clients(
    private_dir: Path,
    canonical_contract_path: Path,
) -> tuple[ShijiuLiveClient, UiContextReadClient, dict[str, Any]]:
    token, secret, browser_evidence = load_verified_browser_credentials(
        private_dir, canonical_contract_path
    )
    client = ShijiuLiveClient(
        token,
        secret,
        write_confirmation=COMPLEX_WRITE_CONFIRMATION,
    )
    ui = UiContextReadClient(private_dir, canonical_contract_path)
    if client.token != ui.query_token or client.secret != ui.base_form.get("secret"):
        raise LiveImportError("canonical CREATE and UI-context session credentials differ")
    return client, ui, browser_evidence
