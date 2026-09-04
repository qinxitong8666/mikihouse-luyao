from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Any

from .shijiu_complex_import import UiContextReadClient, _row_id
from .shijiu_import import now, recursively_find_skus
from .shijiu_live_import import DETAIL_PATH, LIST_PATH, LiveImportError, _first_observation, _split_urls


CAPACITY_AUDIT_MODE = "SHIJIU_READ_ONLY_RICH_MEDIA_CAPACITY_EMPIRICAL_AUDIT"


def _identity_hash(product_id: str) -> str:
    return hashlib.sha256(str(product_id).encode("utf-8")).hexdigest()


def _string_size(value: Any) -> tuple[int, int]:
    text = "" if value is None else str(value)
    return len(text), len(text.encode("utf-8"))


def _html_image_urls(html: str) -> list[str]:
    return [
        match.strip()
        for match in re.findall(
            r"<img\b[^>]*?\bsrc=[\"']([^\"']+)[\"']",
            html,
            flags=re.I,
        )
        if match.strip()
    ]


def target_product_capacity_metrics(detail: dict[str, Any]) -> dict[str, Any]:
    broadcast_value = _first_observation(detail, ("broadcast",))
    detail_pics_value = _first_observation(detail, ("good_detail_pics",))
    details = str(_first_observation(detail, ("good_details",)) or "")
    broadcast = _split_urls(broadcast_value)
    detail_pics = _split_urls(detail_pics_value)
    detail_html_images = _html_image_urls(details)
    details_chars, details_bytes = _string_size(details)
    broadcast_text = ",".join(broadcast) if isinstance(broadcast_value, list) else str(
        broadcast_value or ""
    )
    detail_pics_text = ",".join(detail_pics) if isinstance(detail_pics_value, list) else str(
        detail_pics_value or ""
    )
    broadcast_chars, broadcast_bytes = _string_size(broadcast_text)
    detail_pics_chars, detail_pics_bytes = _string_size(detail_pics_text)
    all_media = broadcast + detail_pics + detail_html_images
    return {
        "sku_count": len(recursively_find_skus(detail)),
        "broadcast": {
            "character_count": broadcast_chars,
            "utf8_byte_count": broadcast_bytes,
            "url_count": len(broadcast),
            "image_count": len(broadcast),
            "max_url_character_count": max(map(len, broadcast), default=0),
        },
        "good_detail_pics": {
            "character_count": detail_pics_chars,
            "utf8_byte_count": detail_pics_bytes,
            "url_count": len(detail_pics),
            "image_count": len(detail_pics),
            "max_url_character_count": max(map(len, detail_pics), default=0),
        },
        "good_details": {
            "character_count": details_chars,
            "utf8_byte_count": details_bytes,
            "url_count": len(re.findall(r"https?://[^\"'<>\s]+", details)),
            "image_count": len(detail_html_images),
            "image_tag_count": len(re.findall(r"<img\b", details, flags=re.I)),
            "max_url_character_count": max(map(len, detail_html_images), default=0),
        },
        "total_distinct_media_url_count": len(set(all_media)),
    }


def _percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def _sku_distribution(values: list[int]) -> dict[str, Any]:
    buckets = Counter()
    for value in values:
        if value == 1:
            buckets["1"] += 1
        elif value <= 4:
            buckets["2-4"] += 1
        elif value <= 11:
            buckets["5-11"] += 1
        elif value <= 14:
            buckets["12-14"] += 1
        elif value <= 24:
            buckets["15-24"] += 1
        else:
            buckets["25+"] += 1
    return {
        "product_count": len(values),
        "minimum": min(values, default=0),
        "maximum": max(values, default=0),
        "p50": _percentile(values, 0.50),
        "p90": _percentile(values, 0.90),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "buckets": {key: buckets.get(key, 0) for key in ("1", "2-4", "5-11", "12-14", "15-24", "25+")},
    }


def _cohort(
    product_id: str,
    *,
    non_miki_test_ids: set[str],
    legacy_ids: set[str],
    mapped_mikihouse_ids: set[str],
) -> str:
    if product_id in non_miki_test_ids:
        return "NON_MIKIHOUSE_BROWSER_TEST"
    if product_id in legacy_ids:
        return "LEGACY_REFERENCE_ONLY"
    if product_id in mapped_mikihouse_ids:
        return "MAPPED_MIKIHOUSE_READ_ONLY_REFERENCE"
    return "OTHER_UI_CONTEXT_READABLE_PRODUCT"


def _max_observation(
    observations: list[dict[str, Any]],
    path: tuple[str, ...],
) -> dict[str, Any]:
    def value(row: dict[str, Any]) -> int:
        current: Any = row["metrics"]
        for key in path:
            current = current[key]
        return int(current)

    winner = max(observations, key=lambda row: (value(row), row["identity_sha256"]))
    return {
        "value": value(winner),
        "source_cohort": winner["cohort"],
        "product_identity_sha256": winner["identity_sha256"],
    }


def build_capacity_audit_report(
    ui: UiContextReadClient,
    *,
    legacy_product_ids: set[str],
    non_miki_test_product_ids: set[str],
    mapped_mikihouse_product_ids: set[str],
    historical_payload_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Audit a deterministic target sample and explicit references without mutation."""
    request_start = len(ui.requests)
    visible_rows, list_coverage = ui.sample_context_rows(good_type="")
    visible_ids = {_row_id(row) for row in visible_rows if _row_id(row)}
    explicit_ids = (
        set(legacy_product_ids)
        | set(non_miki_test_product_ids)
        | set(mapped_mikihouse_product_ids)
    )
    all_ids = sorted(visible_ids | explicit_ids, key=lambda value: (len(value), value))
    if not all_ids:
        raise LiveImportError("capacity audit found no safely readable target products")
    observations: list[dict[str, Any]] = []
    for product_id in all_ids:
        detail = ui.product_detail(product_id)
        observations.append({
            "identity_sha256": _identity_hash(product_id),
            "cohort": _cohort(
                product_id,
                non_miki_test_ids=non_miki_test_product_ids,
                legacy_ids=legacy_product_ids,
                mapped_mikihouse_ids=mapped_mikihouse_product_ids,
            ),
            "metrics": target_product_capacity_metrics(detail),
        })
    current_requests = ui.requests[request_start:]
    if any(
        row.get("semantic_operation") != "read"
        or row.get("path") not in {LIST_PATH, DETAIL_PATH}
        for row in current_requests
    ):
        raise LiveImportError("capacity audit observed a prohibited target operation")
    field_paths = {
        "sku_count": ("sku_count",),
        "broadcast_character_count": ("broadcast", "character_count"),
        "broadcast_utf8_byte_count": ("broadcast", "utf8_byte_count"),
        "broadcast_url_count": ("broadcast", "url_count"),
        "broadcast_image_count": ("broadcast", "image_count"),
        "good_detail_pics_character_count": ("good_detail_pics", "character_count"),
        "good_detail_pics_utf8_byte_count": ("good_detail_pics", "utf8_byte_count"),
        "good_detail_pics_url_count": ("good_detail_pics", "url_count"),
        "good_detail_pics_image_count": ("good_detail_pics", "image_count"),
        "good_details_character_count": ("good_details", "character_count"),
        "good_details_utf8_byte_count": ("good_details", "utf8_byte_count"),
        "good_details_url_count": ("good_details", "url_count"),
        "good_details_image_count": ("good_details", "image_count"),
    }
    empirical_maxima = {
        name: _max_observation(observations, path) for name, path in field_paths.items()
    }
    cohort_counts = Counter(row["cohort"] for row in observations)
    return {
        "schema_version": 1,
        "generated_at": now(),
        "status": "COMPLETED_READ_ONLY",
        "mode": CAPACITY_AUDIT_MODE,
        "target": "SHIJIU",
        "scope": {
            "ui_context_list_coverage": list_coverage,
            "ui_visible_product_count": len(visible_ids),
            "explicit_reference_product_count": len(explicit_ids),
            "unique_detail_product_count": len(observations),
            "cohort_product_counts": dict(sorted(cohort_counts.items())),
            "all_ui_declared_rows_read": list_coverage["all_declared_rows_enumerated"],
            "deterministic_evenly_spaced_page_sample_completed": list_coverage[
                "sampling_is_deterministic"
            ],
            "legacy_reference_mode": "READ_ONLY_NO_IDENTITY_RECONCILIATION",
        },
        "target_empirical_observed_maxima": empirical_maxima,
        "target_sku_count_distribution": _sku_distribution(
            [row["metrics"]["sku_count"] for row in observations]
        ),
        "comparison_table": [
            *historical_payload_rows,
            {
                "label": "TARGET_OBSERVED_PER_FIELD_MAXIMUM_COMPOSITE",
                "outcome": "READ_ONLY_EMPIRICAL_OBSERVATION",
                "metrics": {key: row["value"] for key, row in empirical_maxima.items()},
                "note": "Each maximum may come from a different target product; identities are SHA-256 only.",
            },
        ],
        "interpretation": {
            "empirical_observed_maximum": (
                "Largest value returned by this deterministic captured-UI-context page sample plus "
                "explicit non-MIKIHOUSE test, legacy, and mapped read-only references."
            ),
            "target_global_maximum": "NOT_CLAIMED; UNSAMPLED PRODUCTS MAY BE LARGER",
            "server_hard_limit": "NOT_PROVEN_AND_NOT_INFERRED_FROM_OBSERVED_MAXIMA",
            "create_acceptance_implication": (
                "Existing readback scale demonstrates stored/readable data only; it does not prove "
                "the current canonical CREATE endpoint accepts the same scale."
            ),
        },
        "request_counts": {
            "read": len(current_requests),
            "goods_index": sum(row.get("path") == LIST_PATH for row in current_requests),
            "get_format_info": sum(
                row.get("path") == DETAIL_PATH for row in current_requests
            ),
            "write": 0,
            "image_upload": 0,
            "product_create": 0,
            "update": 0,
        },
        "target_mutation_requests_sent": 0,
        "product_names_persisted": False,
        "raw_urls_persisted": False,
        "product_ids_persisted": False,
        "authentication_values_persisted": False,
    }
