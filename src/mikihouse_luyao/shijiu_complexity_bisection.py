from __future__ import annotations

import copy
import hashlib
import json
import re
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .shijiu_canonical_create import load_verified_browser_credentials
from .shijiu_complex_import import (
    TARGET_CATEGORY_ID,
    ComplexLiveBatchRunner,
    UiContextReadClient,
    _metrics,
)
from .shijiu_import import (
    EXPECTED_SPECIAL_COUNT,
    PDF_SPECIAL_EXCLUDED_REASON,
    SOURCE_CODE,
    content_sha256,
    map_product_to_shijiu,
    now,
)
from .shijiu_live_import import LiveImportError, ShijiuLiveClient, _resolve_payload


BISECTION_BATCH_SIZE = 2
BISECTION_MODE = "COMPLEXITY_BISECTION_2_REAL_IMPORT_VALIDATION"
BISECTION_WRITE_CONFIRMATION = "MIKIHOUSE_COMPLEXITY_BISECTION_2_REAL_IMPORT"
SUCCESSFUL_REFERENCE_PRODUCT = "36-2001-572"
FAILED_REFERENCE_PRODUCT = "13-9310-490"
ORIGINAL_FROZEN_FIVE = {
    "13-9310-490",
    "10-1829-685",
    "10-8227-686",
    "00-4000-054",
    "10-8223-684",
}
ALL_PREVIOUS_CREATE_PRODUCTS = {
    "00-1000-028",
    "17-1366-244",
    "36-2001-572",
    *ORIGINAL_FROZEN_FIVE,
}


def _compact_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _split_urls(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _string_field_maxima(value: Any) -> list[dict[str, Any]]:
    observations: dict[str, list[str]] = defaultdict(list)

    def walk(current: Any, path: str) -> None:
        if isinstance(current, dict):
            for key, child in current.items():
                walk(child, f"{path}.{key}" if path else str(key))
        elif isinstance(current, list):
            for child in current:
                walk(child, f"{path}[]")
        elif isinstance(current, str):
            observations[path].append(current)

    walk(value, "")
    rows = []
    for path, strings in observations.items():
        longest_chars = max(strings, key=len)
        longest_bytes = max(strings, key=lambda item: len(item.encode("utf-8")))
        rows.append({
            "path": path,
            "observed_value_count": len(strings),
            "max_character_count": len(longest_chars),
            "max_utf8_byte_count": len(longest_bytes.encode("utf-8")),
        })
    return sorted(
        rows,
        key=lambda row: (-row["max_utf8_byte_count"], -row["max_character_count"], row["path"]),
    )


def payload_complexity_metrics(
    payload: dict[str, Any],
    *,
    token: str | None = None,
    secret: str | None = None,
) -> dict[str, Any]:
    specs = list(payload.get("spec_name") or [])
    skus = list(payload.get("sku_info") or [])
    broadcast = str(payload.get("broadcast") or "")
    details = str(payload.get("good_details") or "")
    detail_pics = str(payload.get("good_detail_pics") or "")
    business_bytes = _compact_bytes(payload)
    wire_byte_count = None
    auth_overhead = None
    if token is not None and secret is not None:
        wire = _compact_bytes({"secret": secret, "token": token, **payload})
        wire_byte_count = len(wire)
        auth_overhead = len(wire) - len(business_bytes)
    option_counts = [len(row.get("son_name") or []) for row in specs]
    maxima = _string_field_maxima(payload)
    return {
        "business_payload_utf8_byte_count": len(business_bytes),
        "wire_body_utf8_byte_count": wire_byte_count,
        "wire_auth_envelope_overhead_byte_count": auth_overhead,
        "wire_credentials_persisted": False,
        "payload_sha256": content_sha256(payload),
        "sku_info_count": len(skus),
        "spec_dimension_count": len(specs),
        "spec_option_count_total": sum(option_counts),
        "spec_option_count_by_dimension": option_counts,
        "spec_max_options_in_one_dimension": max(option_counts, default=0),
        "broadcast": {
            "url_count": len(_split_urls(broadcast)),
            "character_count": len(broadcast),
            "utf8_byte_count": len(broadcast.encode("utf-8")),
            "max_url_character_count": max(map(len, _split_urls(broadcast)), default=0),
        },
        "good_details": {
            "character_count": len(details),
            "utf8_byte_count": len(details.encode("utf-8")),
            "image_tag_count": len(re.findall(r"<img\b", details, flags=re.I)),
            "embedded_url_count": len(re.findall(r"https?://[^\"'<>\s]+", details)),
        },
        "good_detail_pics": {
            "url_count": len(_split_urls(detail_pics)),
            "character_count": len(detail_pics),
            "utf8_byte_count": len(detail_pics.encode("utf-8")),
            "max_url_character_count": max(map(len, _split_urls(detail_pics)), default=0),
        },
        "master_graph_character_count": len(str(payload.get("master_graph") or "")),
        "top_level_string_field_maximum": next(
            (row for row in maxima if "[]" not in row["path"]), None
        ),
        "all_string_field_maxima": maxima,
    }


def _mapped_item(
    master: dict[str, Any],
    number: str,
    special: set[str],
    target_category: dict[str, Any],
) -> dict[str, Any]:
    product = next(
        (row for row in master.get("products") or [] if row.get("product_number") == number),
        None,
    )
    if not product:
        raise LiveImportError(f"reference product missing from master: {number}")
    return map_product_to_shijiu(product, target_category, excluded_product_numbers=special)


def build_payload_scale_comparison(
    master: dict[str, Any],
    special: set[str],
    target_category: dict[str, Any],
    canonical_checkpoint: dict[str, Any],
    failed_checkpoint: dict[str, Any],
    *,
    token: str | None = None,
    secret: str | None = None,
) -> dict[str, Any]:
    success_item = _mapped_item(
        master, SUCCESSFUL_REFERENCE_PRODUCT, special, target_category
    )
    failed_item = _mapped_item(master, FAILED_REFERENCE_PRODUCT, special, target_category)
    failed_record = (failed_checkpoint.get("records") or {}).get(FAILED_REFERENCE_PRODUCT) or {}
    success_payload = _resolve_payload(success_item, canonical_checkpoint.get("image_uploads") or {})
    failed_payload = _resolve_payload(failed_item, failed_record.get("image_uploads") or {})
    if content_sha256(success_payload) != canonical_checkpoint.get("resolved_payload_sha256"):
        raise LiveImportError("successful reference resolved payload hash drift")
    if content_sha256(failed_payload) != failed_record.get("resolved_payload_sha256"):
        raise LiveImportError("failed reference resolved payload hash drift")
    success = payload_complexity_metrics(success_payload, token=token, secret=secret)
    failed = payload_complexity_metrics(failed_payload, token=token, secret=secret)
    numeric_paths = {
        "business_payload_utf8_byte_count": (
            success["business_payload_utf8_byte_count"], failed["business_payload_utf8_byte_count"]
        ),
        "wire_body_utf8_byte_count": (
            success["wire_body_utf8_byte_count"], failed["wire_body_utf8_byte_count"]
        ),
        "sku_info_count": (success["sku_info_count"], failed["sku_info_count"]),
        "spec_dimension_count": (
            success["spec_dimension_count"], failed["spec_dimension_count"]
        ),
        "spec_option_count_total": (
            success["spec_option_count_total"], failed["spec_option_count_total"]
        ),
        "broadcast_url_count": (
            success["broadcast"]["url_count"], failed["broadcast"]["url_count"]
        ),
        "broadcast_character_count": (
            success["broadcast"]["character_count"], failed["broadcast"]["character_count"]
        ),
        "good_details_character_count": (
            success["good_details"]["character_count"],
            failed["good_details"]["character_count"],
        ),
        "good_details_image_tag_count": (
            success["good_details"]["image_tag_count"],
            failed["good_details"]["image_tag_count"],
        ),
        "good_detail_pics_url_count": (
            success["good_detail_pics"]["url_count"],
            failed["good_detail_pics"]["url_count"],
        ),
    }
    deltas = {}
    for name, (left, right) in numeric_paths.items():
        deltas[name] = {
            "successful": left,
            "failed": right,
            "absolute_delta": None if left is None or right is None else right - left,
            "failed_to_success_ratio": (
                None if left in (None, 0) or right is None else round(right / left, 4)
            ),
        }
    return {
        "schema_version": 1,
        "generated_at": now(),
        "mode": "OFFLINE_CREATE_PAYLOAD_SCALE_COMPARISON",
        "successful_reference": {
            "product_number": SUCCESSFUL_REFERENCE_PRODUCT,
            "create_attempts": canonical_checkpoint.get("create_attempts"),
            "persistence_verified": bool(canonical_checkpoint.get("mapping_persisted")),
            "metrics": success,
        },
        "failed_reference": {
            "product_number": FAILED_REFERENCE_PRODUCT,
            "create_attempts": failed_record.get("create_attempts"),
            "persistence_verified": False,
            "retry_allowed": False,
            "metrics": failed,
        },
        "deltas": deltas,
        "credentials_used_only_for_wire_byte_count": token is not None and secret is not None,
        "credential_values_persisted": False,
        "target_requests_sent": 0,
    }


def build_orphan_asset_register(failed_checkpoint_path: Path) -> dict[str, Any]:
    raw = failed_checkpoint_path.read_bytes()
    checkpoint = json.loads(raw)
    record = (checkpoint.get("records") or {}).get(FAILED_REFERENCE_PRODUCT) or {}
    assets = []
    for reference, row in sorted(
        (record.get("image_uploads") or {}).items(), key=lambda pair: pair[1].get("order", 0)
    ):
        target_url = str(row.get("target_url") or "")
        if row.get("status") != "UPLOADED" or not target_url.startswith("https://"):
            raise LiveImportError("failed reference does not contain 42 complete COS uploads")
        parsed = urllib.parse.urlparse(target_url)
        assets.append({
            "upload_reference": reference,
            "order": row.get("order"),
            "role": row.get("role"),
            "source_url_sha256": row.get("source_url_sha256"),
            "target_url": target_url,
            "target_url_sha256": hashlib.sha256(target_url.encode("utf-8")).hexdigest(),
            "target_host": parsed.netloc,
            "status": "UPLOADED_REUSABLE_ORPHAN",
        })
    if len(assets) != 42:
        raise LiveImportError(f"expected 42 reusable orphan COS assets, got {len(assets)}")
    return {
        "schema_version": 1,
        "generated_at": now(),
        "source": SOURCE_CODE,
        "product_number": FAILED_REFERENCE_PRODUCT,
        "asset_count": len(assets),
        "state": "REUSABLE_ORPHAN_ASSETS_RETAINED",
        "retry_product_create_allowed": False,
        "delete_allowed": False,
        "reupload_allowed_in_this_batch": False,
        "reuse_scope": "future separately authorized reconciliation for the same product only",
        "source_checkpoint_sha256": hashlib.sha256(raw).hexdigest(),
        "assets": assets,
        "sensitive_values_included": False,
    }


def audit_wawu_multisku_evidence(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    evidence = json.loads(raw)
    imported = evidence.get("import") or {}
    rows = imported.get("compact_items") or []
    verified = [
        row for row in rows
        if row.get("status") == "VERIFIED_DONE"
        and row.get("reason") == "CREATED_UNIQUE_READBACK"
        and row.get("write_count") == 1
    ]
    if imported.get("failed") != 0 or len(verified) != imported.get("created"):
        raise LiveImportError("WAWU reference does not prove unique readback for every CREATE")
    maximum = max((int(row.get("sku_count") or 0) for row in verified), default=0)
    return {
        "evidence_kind": "existing_read_only_committed_WAWU_to_Shijiu_CREATE_result",
        "evidence_sha256": hashlib.sha256(raw).hexdigest(),
        "evidence_commit": "5ed5dbc",
        "verified_created_product_count": len(verified),
        "verified_created_sku_count_total": sum(int(row["sku_count"]) for row in verified),
        "maximum_verified_sku_count_per_created_product": maximum,
        "known_acceptable_CREATE_scale_conclusion": (
            f"Shijiu has accepted and uniquely read back at least one {maximum}-SKU CREATE"
        ),
        "wawu_upstream_semantics_reused": False,
        "target_requests_sent": 0,
        "sensitive_values_included": False,
    }


def audit_shijiu_legacy_multisku_evidence(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    evidence = json.loads(raw)
    rows = evidence.get("sample_schemas") or []
    if (
        evidence.get("classification") != "legacy_reference_only"
        or evidence.get("passed") is not True
        or evidence.get("sku_binding_attempted") is not False
        or len(rows) != evidence.get("sample_size")
    ):
        raise LiveImportError("legacy reference audit is not a valid read-only structure sample")
    maximum = max((int(row.get("detail_sku_count") or 0) for row in rows), default=0)
    return {
        "evidence_kind": "existing_read_only_Shijiu_legacy_product_structure",
        "evidence_sha256": hashlib.sha256(raw).hexdigest(),
        "sample_product_count": len(rows),
        "maximum_readable_sku_count_per_existing_product": maximum,
        "known_storage_readback_scale_conclusion": (
            f"Shijiu currently stores and getFormatInfo reads at least one {maximum}-SKU product"
        ),
        "proves_current_canonical_CREATE_acceptance": False,
        "identity_reconciliation_attempted": False,
        "legacy_reference_touched_by_current_run": False,
        "target_requests_sent": 0,
        "sensitive_values_included": False,
    }


def _candidate_pool(
    master: dict[str, Any],
    special: set[str],
    mapping: dict[str, Any],
    target_category: dict[str, Any],
) -> list[dict[str, Any]]:
    names = Counter(str(row.get("name") or "").strip() for row in master.get("products") or [])
    rows = []
    for product in master.get("products") or []:
        number = str(product.get("product_number") or "")
        mapping_row = (mapping.get("products") or {}).get(number) or {}
        if (
            not number
            or number in special
            or number in ALL_PREVIOUS_CREATE_PRODUCTS
            or not product.get("active")
            or mapping_row.get("shijiu_product_id") not in (None, "")
        ):
            continue
        mapped = map_product_to_shijiu(
            product, target_category, excluded_product_numbers=special
        )
        if not mapped.get("publish_ready"):
            continue
        metrics = _metrics(product)
        payload = mapped["shijiu_payload_preview"]
        rows.append({
            "product": product,
            "mapped": mapped,
            "name_unique_in_source": names[str(product.get("name") or "").strip()] == 1,
            "good_details_character_count": len(str(payload.get("good_details") or "")),
            "detail_image_count": len(_split_urls(payload.get("good_detail_pics"))),
            **metrics,
        })
    return rows


def select_bisection_batch(
    master: dict[str, Any],
    special: set[str],
    mapping: dict[str, Any],
    target_category: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if len(special) != EXPECTED_SPECIAL_COUNT:
        raise LiveImportError(f"expected {EXPECTED_SPECIAL_COUNT} {PDF_SPECIAL_EXCLUDED_REASON} rows")
    pool = _candidate_pool(master, special, mapping, target_category)
    image_candidates = [row for row in pool if 2 <= row["variant_count"] <= 4]
    if not image_candidates:
        raise LiveImportError("no eligible 2-4 variant image/detail-scale candidate")
    image_probe = min(image_candidates, key=lambda row: (
        0 if row["name_unique_in_source"] else 1,
        -row["image_count"],
        -row["good_details_character_count"],
        row["product"]["product_number"],
    ))
    sku_candidates = [
        row for row in pool
        if 12 <= row["variant_count"] <= 24
        and row["product"]["product_number"] != image_probe["product"]["product_number"]
    ]
    if not sku_candidates:
        raise LiveImportError("no eligible 12-24 variant SKU-scale candidate")
    sku_probe = min(sku_candidates, key=lambda row: (
        row["image_count"],
        row["good_details_character_count"],
        row["spec_dimension_count"] if "spec_dimension_count" in row else 99,
        row["product"]["product_number"],
    ))
    selected = [
        ("IMAGE_DETAIL_SCALE_2_TO_4_VARIANTS", image_probe),
        ("SKU_SCALE_12_TO_24_VARIANTS", sku_probe),
    ]
    items = [copy.deepcopy(row["mapped"]) for _, row in selected]
    selection = {
        "schema_version": 1,
        "generated_at": now(),
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "mode": BISECTION_MODE,
        "selection_policy": (
            "probe 1: unique name then maximum image/detail scale among 2-4 variants; "
            "probe 2: minimum image/detail scale among 12-24 variants"
        ),
        "execution_order_is_binding": True,
        "second_requires_first_strong_readback_and_mapping": True,
        "fixed_target_category_id": TARGET_CATEGORY_ID,
        "pdf_special_exclusion_count": len(special),
        "hard_prohibited_products": sorted(ALL_PREVIOUS_CREATE_PRODUCTS),
        "original_complex_batch_frozen": True,
        "eligible_pool_count": len(pool),
        "products": [
            {
                "sequence": index,
                "role": role,
                "product_number": row["product"]["product_number"],
                "good_name": row["mapped"]["shijiu_payload_preview"]["good_name"],
                "name_unique_in_source": row["name_unique_in_source"],
                "variant_count": row["variant_count"],
                "available_variant_count": row["available_variant_count"],
                "color_count": row["color_count"],
                "size_count": row["size_count"],
                "image_count": row["image_count"],
                "gallery_or_detail_image_count": row["gallery_or_detail_image_count"],
                "detail_image_count": row["detail_image_count"],
                "good_details_character_count": row["good_details_character_count"],
                "payload_sha256": row["mapped"]["payload_sha256"],
            }
            for index, (role, row) in enumerate(selected, start=1)
        ],
    }
    numbers = {item["product_number"] for item in items}
    if (
        len(items) != BISECTION_BATCH_SIZE
        or numbers & special
        or numbers & ALL_PREVIOUS_CREATE_PRODUCTS
    ):
        raise LiveImportError("complexity bisection selection boundary failed")
    return items, selection


def load_frozen_bisection_items(
    master: dict[str, Any],
    special: set[str],
    target_category: dict[str, Any],
    selection: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = selection.get("products") or []
    numbers = [str(row.get("product_number") or "") for row in rows]
    if (
        selection.get("mode") != BISECTION_MODE
        or selection.get("fixed_target_category_id") != TARGET_CATEGORY_ID
        or len(numbers) != BISECTION_BATCH_SIZE
        or len(set(numbers)) != BISECTION_BATCH_SIZE
        or set(numbers) & special
        or set(numbers) & ALL_PREVIOUS_CREATE_PRODUCTS
    ):
        raise LiveImportError("invalid frozen complexity-bisection selection")
    by_number = {str(row.get("product_number") or ""): row for row in master.get("products") or []}
    items = []
    for selection_row in rows:
        number = str(selection_row["product_number"])
        product = by_number.get(number)
        if not product or not product.get("active"):
            raise LiveImportError(f"frozen bisection product missing/inactive: {number}")
        item = map_product_to_shijiu(
            product, target_category, excluded_product_numbers=special
        )
        if not item.get("publish_ready") or item["payload_sha256"] != selection_row["payload_sha256"]:
            raise LiveImportError(f"frozen bisection payload drift: {number}")
        items.append(item)
    return items


def make_bisection_clients(
    private_dir: Path, canonical_contract_path: Path
) -> tuple[ShijiuLiveClient, UiContextReadClient, dict[str, Any]]:
    token, secret, evidence = load_verified_browser_credentials(private_dir, canonical_contract_path)
    client = ShijiuLiveClient(token, secret, write_confirmation=BISECTION_WRITE_CONFIRMATION)
    ui = UiContextReadClient(private_dir, canonical_contract_path)
    if client.token != ui.query_token or client.secret != ui.base_form.get("secret"):
        raise LiveImportError("canonical CREATE and UI-context session credentials differ")
    return client, ui, evidence


def build_bisection_diagnosis(
    checkpoint: dict[str, Any],
    *,
    items: list[dict[str, Any]] | None = None,
    token: str | None = None,
    secret: str | None = None,
    scale_comparison: dict[str, Any] | None = None,
) -> dict[str, Any]:
    records = list((checkpoint.get("records") or {}).values())
    passed = [
        row.get("state") in {"READBACK_VERIFIED", "READBACK_VERIFIED_AFTER_BATCH_STOP"}
        for row in records
    ]
    create_attempts = [int(row.get("create_attempts") or 0) for row in records]
    if passed == [True, True]:
        decision = "NEITHER_SCALE_ALONE_EXPLAINS_13_9310_490_FAILURE"
        explanation = (
            "Both the image/detail-heavy low-SKU probe and the image-light high-SKU probe "
            "persisted with full readback; investigate combined scale or other business values."
        )
    elif passed and not passed[0] and create_attempts[0] == 1:
        decision = "IMAGE_OR_DETAIL_SCALE_SUSPECTED_SKU_PROBE_NOT_RUN"
        explanation = (
            "The 2-4 variant image/detail-heavy probe was not strongly persisted; fail-closed "
            "sequencing prohibited the SKU probe."
        )
    elif len(passed) == 2 and passed[0] and not passed[1] and create_attempts[1] == 1:
        decision = "SKU_SCALE_SUSPECTED"
        explanation = (
            "The image/detail-heavy low-SKU probe passed, while the image-light 12-24 SKU "
            "probe was not strongly persisted."
        )
    else:
        decision = "INCONCLUSIVE_NO_SCALE_CREATE_OUTCOME"
        explanation = "A pre-CREATE/upload/session failure prevents a scale diagnosis."
    result = {
        "schema_version": 1,
        "generated_at": now(),
        "decision": decision,
        "explanation": explanation,
        "probe_passed": passed,
        "create_attempts": create_attempts,
        "failed_reference_retried": False,
        "original_five_continued": False,
        "next_20_plan_generated": False,
        "legacy_reference_touched": False,
        "sensitive_values_included": False,
    }
    if items is not None:
        by_number = {item["product_number"]: item for item in items}
        probe_results = []
        for number, record in (checkpoint.get("records") or {}).items():
            item = by_number[number]
            uploads = record.get("image_uploads") or {}
            complete = (
                len(uploads) == len(item.get("image_upload_plan") or [])
                and all(row.get("status") == "UPLOADED" for row in uploads.values())
            )
            if complete:
                payload = _resolve_payload(item, uploads)
                metric_kind = "actual_resolved_CREATE_payload"
            else:
                payload = item["shijiu_payload_preview"]
                metric_kind = "planned_placeholder_payload_not_sent"
            probe_results.append({
                "product_number": number,
                "state": record.get("state"),
                "create_attempts": record.get("create_attempts", 0),
                "uploaded_image_count": sum(
                    row.get("status") == "UPLOADED" for row in uploads.values()
                ),
                "mapping_persisted": record.get("mapping_persisted", False),
                "uploaded_asset_disposition": (
                    "BOUND_TO_VERIFIED_PRODUCT"
                    if record.get("mapping_persisted")
                    else (
                        "REUSABLE_ORPHAN_RETAINED_NO_DELETE_NO_REUPLOAD"
                        if complete
                        else "NO_COMPLETE_UPLOAD_SET"
                    )
                ),
                "payload_metric_kind": metric_kind,
                "payload_metrics": payload_complexity_metrics(
                    payload, token=token, secret=secret
                ),
            })
        result["probe_results"] = probe_results
    if scale_comparison is not None:
        result["decision_basis"] = {
            "known_verified_CREATE_sku_maximum": (
                scale_comparison.get("known_shijiu_multisku_evidence") or {}
            ).get("maximum_verified_sku_count_per_created_product"),
            "known_existing_readable_sku_maximum": (
                scale_comparison.get("known_shijiu_existing_product_scale") or {}
            ).get("maximum_readable_sku_count_per_existing_product"),
            "failed_reference_business_payload_bytes": (
                scale_comparison.get("failed_reference", {}).get("metrics", {})
            ).get("business_payload_utf8_byte_count"),
            "failed_reference_sku_count": (
                scale_comparison.get("failed_reference", {}).get("metrics", {})
            ).get("sku_info_count"),
            "failed_reference_broadcast_url_count": (
                scale_comparison.get("failed_reference", {}).get("metrics", {}).get("broadcast", {})
            ).get("url_count"),
            "causality_status": "SUSPECTED_BY_CONTROLLED_PROBE_NOT_PROVEN_AS_SERVER_LIMIT",
        }
    return result


class ComplexityBisectionRunner(ComplexLiveBatchRunner):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(
            *args,
            expected_batch_size=BISECTION_BATCH_SIZE,
            expected_confirmation=BISECTION_WRITE_CONFIRMATION,
            mode=BISECTION_MODE,
            prohibited_product_numbers=ALL_PREVIOUS_CREATE_PRODUCTS,
            **kwargs,
        )
