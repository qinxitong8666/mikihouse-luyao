from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
import math
import re
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from .catalog import calculate_mini_program_price_jpy
from .csv_input import read_product_numbers
from .shijiu_import import (
    _classification_name,
    content_sha256,
    load_category_map,
    map_product_to_shijiu,
)
from .shijiu_duplicate_name_identity import (
    analyze_duplicate_names,
    audit_price_outside_configured_range,
)
from .shijiu_staged_media_import import image_reference_sets, stage_plan
from .stable_catalog import (
    NON_SELLABLE_SERVICE_OR_ADDON,
    PDF_SPECIAL,
    PERMANENT_NON_SELLABLE_PRODUCT_NUMBERS,
    REVIEW_REQUIRED,
    STABLE,
    assess_product_stability,
)
from .stable_sync import IncompleteCrawlError, validate_complete_snapshot


PLAN_SCHEMA_VERSION = 1
SOURCE = "MIKIHOUSE"
TARGET = "SHIJIU"
TARGET_CATEGORY_ID = 294884
EXPECTED_SPECIAL_COUNT = 351
PLANNING_STATUS = "PLANNING_ONLY"
WRITE_BLOCKED_STATUS = "SHIJIU_WRITE_BLOCKED_CONCURRENT_WRITER"
OLD_PLAN_RELATIVE_PATH = "deliverables/shijiu_import/richtext_e2e_next_20_frozen_plan.json"
NEW_PILOT_PRODUCT_COUNT = 20
PRODUCT_NUMBER_RE = re.compile(r"^[0-9]{2}-[0-9]{4}-[0-9]{3}$")

TIER_ORDER = {
    "SIMPLE_LOW_SKU_LOW_MEDIA": 1,
    "STANDARD": 2,
    "MULTI_SKU": 3,
    "RICH_MEDIA": 4,
    "HIGH_COMPLEXITY": 5,
}
TIER_BATCH_SIZE = {
    "SIMPLE_LOW_SKU_LOW_MEDIA": 50,
    "STANDARD": 30,
    "MULTI_SKU": 15,
    "RICH_MEDIA": 10,
    "HIGH_COMPLEXITY": 5,
}


class InitializationPlanError(RuntimeError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise InitializationPlanError(f"JSON root must be an object: {path}")
    return value


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    if path.suffix == ".gz":
        with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=9) as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
    else:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _distribution(values: Iterable[int]) -> dict[str, int]:
    buckets = {
        "0": 0,
        "1": 0,
        "2_4": 0,
        "5_8": 0,
        "9_16": 0,
        "17_32": 0,
        "33_64": 0,
        "65_PLUS": 0,
    }
    for value in values:
        if value == 0:
            buckets["0"] += 1
        elif value == 1:
            buckets["1"] += 1
        elif value <= 4:
            buckets["2_4"] += 1
        elif value <= 8:
            buckets["5_8"] += 1
        elif value <= 16:
            buckets["9_16"] += 1
        elif value <= 32:
            buckets["17_32"] += 1
        elif value <= 64:
            buckets["33_64"] += 1
        else:
            buckets["65_PLUS"] += 1
    return buckets


def _percentiles(values: list[int]) -> dict[str, int | None]:
    if not values:
        return {"min": None, "p50": None, "p90": None, "p95": None, "max": None}
    ordered = sorted(values)

    def at(percent: float) -> int:
        return ordered[max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percent) - 1))]

    return {
        "min": ordered[0],
        "p50": at(0.50),
        "p90": at(0.90),
        "p95": at(0.95),
        "max": ordered[-1],
    }


def _valid_product_number(value: Any) -> bool:
    return bool(PRODUCT_NUMBER_RE.fullmatch(str(value or "")))


def _collect_product_numbers(value: Any, *, key: str = "") -> set[str]:
    result: set[str] = set()
    if isinstance(value, dict):
        for child_key, child in value.items():
            result.update(_collect_product_numbers(child, key=str(child_key)))
    elif isinstance(value, list):
        for child in value:
            result.update(_collect_product_numbers(child, key=key))
    elif key in {
        "product_number",
        "product_numbers",
        "hard_prohibited_products",
        "historical_prohibited_product_numbers",
    } and _valid_product_number(value):
        result.add(str(value))
    return result


def historical_frozen_product_numbers(root: Path) -> tuple[set[str], list[dict[str, Any]]]:
    numbers: set[str] = set()
    sources: list[dict[str, Any]] = []
    paths = [
        *sorted((root / "config").glob("shijiu_*.json")),
        *sorted((root / "state").glob("shijiu_*checkpoint.json")),
        root / OLD_PLAN_RELATIVE_PATH,
    ]
    for path in paths:
        if not path.exists() or path.name == "shijiu_mappings.json":
            continue
        payload = _read_json(path)
        found = _collect_product_numbers(payload)
        if not found:
            continue
        numbers.update(found)
        sources.append({
            "path": str(path.relative_to(root)),
            "sha256": _file_sha256(path),
            "product_number_count": len(found),
        })
    return numbers, sources


def validate_historical_plan_is_permanently_stale(root: Path) -> dict[str, Any]:
    path = root / OLD_PLAN_RELATIVE_PATH
    payload = _read_json(path)
    if payload.get("status") != "STALE_BUSINESS_RULE_CHANGED" or payload.get("must_never_execute") is not True:
        raise InitializationPlanError("historical 20-product plan lost its permanent stale guard")
    return {
        "path": OLD_PLAN_RELATIVE_PATH,
        "sha256": _file_sha256(path),
        "status": payload["status"],
        "must_never_execute": True,
        "product_numbers": sorted(
            str(row.get("product_number"))
            for row in payload.get("products") or []
            if row.get("product_number")
        ),
    }


def _source_product_fingerprint(product: dict[str, Any]) -> str:
    images = []
    for entry in product.get("ordered_images") or []:
        image = entry.get("image") or {}
        images.append({
            "order": entry.get("order"),
            "role": entry.get("role"),
            "url": image.get("url"),
            "width": image.get("width"),
            "height": image.get("height"),
            "colors": entry.get("colors") or [],
            "variant_skus": entry.get("variant_skus") or [],
        })
    variants = [{
        "sku": row.get("sku"),
        "active": row.get("active"),
        "available_for_sale": row.get("available_for_sale"),
        "selected_options": row.get("selected_options") or [],
        "color": row.get("color"),
        "size": row.get("size"),
        "tax_included_price_jpy": row.get("tax_included_price_jpy"),
        "compare_at_price_jpy": row.get("compare_at_price_jpy"),
        "variant_image": row.get("variant_image"),
        "resolved_image": row.get("resolved_image"),
    } for row in product.get("variants") or [] if row.get("active", True)]
    return content_sha256({
        "product_number": product.get("product_number"),
        "name": product.get("name"),
        "brand": product.get("brand"),
        "product_type": product.get("product_type"),
        "category": product.get("category"),
        "tags": product.get("tags") or [],
        "description": product.get("description"),
        "description_html": product.get("description_html"),
        "product_url": product.get("product_url"),
        "images": images,
        "variants": variants,
    })


def _image_role_counts(product: dict[str, Any]) -> Counter[str]:
    return Counter(str(row.get("role") or "") for row in product.get("ordered_images") or [])


def _image_url(entry: dict[str, Any]) -> str:
    return str((entry.get("image") or {}).get("url") or "").strip()


def _quality_issues(
    product: dict[str, Any],
    *,
    special: set[str],
    global_sku_counts: Counter[str],
    minimum_price: int,
    maximum_price: int,
    richtext_contract: dict[str, Any],
) -> list[dict[str, Any]]:
    number = str(product.get("product_number") or "")
    issues: list[dict[str, Any]] = []

    def add(code: str, detail: Any = None) -> None:
        issues.append({"code": code, "detail": detail})

    if not _valid_product_number(number):
        add("INVALID_PRODUCT_NUMBER", number)
    if not str(product.get("name") or "").strip():
        add("MISSING_PRODUCT_NAME")
    decision = assess_product_stability(product, special)
    if decision.get("status") != STABLE:
        add("NOT_STABLE_AT_PLAN_TIME", decision.get("excluded_reason") or decision.get("status"))
    if product.get("stability", {}).get("status") != STABLE:
        add("STABLE_CATALOG_ANNOTATION_INVALID", product.get("stability"))
    images = list(product.get("ordered_images") or [])
    urls = [_image_url(row) for row in images]
    if not product.get("main_image") or not str((product.get("main_image") or {}).get("url") or ""):
        add("MISSING_MAIN_IMAGE")
    if not images or any(not value for value in urls):
        add("MISSING_ORDERED_IMAGE")
    if len(urls) != len(set(urls)):
        add("DUPLICATE_ORDERED_IMAGE_URL")
    if [row.get("order") for row in images] != list(range(1, len(images) + 1)):
        add("INVALID_IMAGE_ORDER")
    non_https = [url for url in urls if urllib.parse.urlparse(url).scheme.casefold() != "https"]
    if non_https:
        add("NON_HTTPS_IMAGE_RESOURCE", {"count": len(non_https)})

    variants = [row for row in product.get("variants") or [] if row.get("active", True)]
    if not variants:
        add("MISSING_ACTIVE_VARIANTS")
    local_skus: set[str] = set()
    for variant in variants:
        sku = str(variant.get("sku") or "").strip()
        if not sku:
            add("MISSING_VARIANT_SKU")
            continue
        if sku in local_skus:
            add("DUPLICATE_VARIANT_SKU_WITHIN_PRODUCT", sku)
        local_skus.add(sku)
        if global_sku_counts[sku] != 1:
            add("DUPLICATE_VARIANT_SKU_ACROSS_PRODUCTS", {"sku": sku, "count": global_sku_counts[sku]})
        if variant.get("stable_id") != f"{number}::{sku}":
            add("INVALID_VARIANT_STABLE_ID", sku)
        if not variant.get("selected_options") or not str(variant.get("color") or "") or not str(variant.get("size") or ""):
            add("INCOMPLETE_VARIANT_OPTIONS", sku)
        try:
            tax = int(variant["tax_included_price_jpy"])
            mini = int(variant["mini_program_price_jpy"])
        except (KeyError, TypeError, ValueError):
            add("INVALID_VARIANT_PRICE", sku)
            continue
        if not minimum_price <= tax <= maximum_price:
            add("PRICE_OUTSIDE_CONFIGURED_RANGE", {"sku": sku, "tax_included_price_jpy": tax})
        if mini != calculate_mini_program_price_jpy(tax):
            add("MINI_PROGRAM_PRICE_MISMATCH", sku)
        compare_at = variant.get("compare_at_price_jpy")
        if compare_at is not None and int(compare_at) > tax:
            add("PROMOTIONAL_COMPARE_AT_PRICE_LEAK", sku)
        if not str((variant.get("resolved_image") or {}).get("url") or ""):
            add("MISSING_VARIANT_IMAGE", sku)
    details = str(product.get("shijiu_good_details") or "")
    richtext = richtext_contract.get("good_details") or {}
    maximum_characters = int(richtext.get("maximum_characters") or 0)
    if (
        not details
        or not maximum_characters
        or len(details) > maximum_characters
        or re.search(r"<img\b|https?://", details, flags=re.I)
        or richtext.get("embedded_image_tags_allowed") is not False
        or richtext.get("embedded_urls_allowed") is not False
    ):
        add("RICHTEXT_CONTRACT_VIOLATION")
    return issues


def _resource_manifest(item: dict[str, Any]) -> list[dict[str, Any]]:
    broadcast_refs = set(image_reference_sets(item)["all_broadcast"])
    detail_refs = set(image_reference_sets(item)["all_detail"])
    manifest = []
    for row in item.get("image_upload_plan") or []:
        url = str(row.get("source_url") or "")
        reference = str(row.get("upload_reference") or "")
        manifest.append({
            "order": int(row.get("order") or 0),
            "upload_reference": reference,
            "role": str(row.get("role") or ""),
            "colors": row.get("colors") or [],
            "variant_skus": row.get("variant_skus") or [],
            "source_url": url,
            "source_url_sha256": hashlib.sha256(url.encode("utf-8")).hexdigest(),
            "source_width": row.get("source_width"),
            "source_height": row.get("source_height"),
            "included_in_broadcast": reference in broadcast_refs,
            "included_in_good_detail_pics": reference in detail_refs,
            "target_url": None,
            "status": "SOURCE_MANIFEST_ONLY_NOT_DOWNLOADED_NOT_UPLOADED",
        })
    return manifest


def _stage_payload_shape(item: dict[str, Any], stage: dict[str, Any]) -> dict[str, Any]:
    refs = image_reference_sets(item)
    payload = copy.deepcopy(item["shijiu_payload_preview"])
    payload["state"] = "1"
    payload["is_shelf"] = 0
    placeholder = lambda reference: f"{{{{SHIJIU_COS_URL:{reference}}}}}"
    payload["broadcast"] = ",".join(
        placeholder(reference)
        for reference in refs["all_broadcast"][: int(stage["broadcast_count"])]
    )
    payload["good_detail_pics"] = ",".join(
        placeholder(reference)
        for reference in refs["all_detail"][: int(stage["detail_pic_count"])]
    )
    return payload


def _product_stage_plan(product: dict[str, Any], item: dict[str, Any], source_product: dict[str, Any]) -> dict[str, Any]:
    stages = []
    for row in stage_plan(item):
        shape = _stage_payload_shape(item, row)
        stages.append({
            "sequence": row["sequence"],
            "key": row["key"],
            "operation": row["operation"],
            "broadcast_count": row["broadcast_count"],
            "good_detail_pics_count": row["detail_pic_count"],
            "new_resource_references": list(row["new_references"]),
            "sanitized_full_payload_sha256": content_sha256(shape),
            "payload_contains_credentials": False,
            "payload_contains_target_cos_urls": False,
            "mutation_retry_count": 0,
            "required_strong_readback_count": 2,
        })
    refs = image_reference_sets(item)
    variants = [{
        "source_variant_id": row["source_variant_id"],
        "source_variant_sku": row["source_variant_sku"],
        "backend_sku_code": row["backend_sku_code"],
        "shijiu_sku_id": None,
        "color": row["color"],
        "size": row["size"],
        "selected_options": row["selected_options"],
        "available_for_sale": row["available_for_sale"],
        "target_stock": row["stock_mapping"],
        "tax_included_price_jpy": row["tax_included_price_jpy"],
        "target_price_jpy": row["mini_program_price_jpy"],
        "image_upload_reference": row["image_upload_reference"],
    } for row in item["source_variants"]]
    colors = {row["color"] for row in variants if row["color"]}
    sizes = {row["size"] for row in variants if row["size"]}
    return {
        "product_number": product["product_number"],
        "source_product_id": item["source_product_id"],
        "name": product["name"],
        "classification": item["classification"],
        "target_category_id": TARGET_CATEGORY_ID,
        "shijiu_product_id": None,
        "source_content_sha256": product["source_content_sha256"],
        "source_snapshot_product_fingerprint_sha256": _source_product_fingerprint(source_product),
        "sanitized_base_payload_sha256": item["payload_sha256"],
        "good_details_sha256": content_sha256(item["shijiu_payload_preview"]["good_details"]),
        "good_details_contract": "TEXT_OR_LIGHT_HTML_NO_IMG_NO_URL_MAX_1024",
        "create_readback_identity_contract": {
            "good_name_role": "CANDIDATE_SCOPE_ONLY_NOT_BINDING_PROOF",
            "candidate_discovery": "UI_CONTEXT_GOODS_INDEX_EXACT_GOOD_NAME",
            "candidate_verification": "GET_FORMAT_INFO_FOR_EVERY_EXACT_NAME_CANDIDATE",
            "primary_identity": "EXACT_COMPLETE_BACKEND_SKU_CODE_SET",
            "required_auxiliary_conditions": [
                "CATEGORY_294884",
                "EXACT_VARIANT_COUNT",
                "EXACT_SPECIFICATION_STRUCTURE",
                "EXACT_VARIANT_PRICES",
            ],
            "accepted_outcome": "UNIQUE_STRONG_MATCH_ONLY",
            "zero_matches": "NOT_FOUND_FAIL_CLOSED",
            "multiple_matches": "AMBIGUOUS_FAIL_CLOSED",
            "shijiu_sku_id": None,
        },
        "variant_count": len(variants),
        "available_variant_count": sum(row["available_for_sale"] for row in variants),
        "color_count": len(colors),
        "size_count": len(sizes),
        "broadcast_count": len(refs["all_broadcast"]),
        "good_detail_pics_count": len(refs["all_detail"]),
        "cos_resource_count": len(item["image_upload_plan"]),
        "required_create_count": 1,
        "required_update_count": max(0, len(stages) - 1),
        "estimated_readback_count": sum(row["required_strong_readback_count"] for row in stages),
        "variants": variants,
        "resource_manifest": _resource_manifest(item),
        "stages": stages,
        "freshness": {
            "requires_complete_storefront_recrawl_before_first_mutation": True,
            "requires_resource_preflight_immediately_before_each_product": True,
            "regenerate_if_price_stock_variant_or_image_changed": True,
        },
    }


def _complexity_tier(plan: dict[str, Any]) -> str:
    variants = int(plan["variant_count"])
    images = int(plan["broadcast_count"])
    details = int(plan["good_detail_pics_count"])
    updates = int(plan["required_update_count"])
    if variants > 24 or images > 48 or details > 32 or updates > 8:
        return "HIGH_COMPLEXITY"
    if images > 20 or details > 8 or updates > 3:
        return "RICH_MEDIA"
    if variants > 8:
        return "MULTI_SKU"
    if variants <= 2 and images <= 8 and details <= 4 and updates <= 1:
        return "SIMPLE_LOW_SKU_LOW_MEDIA"
    return "STANDARD"


def _complexity_score(plan: dict[str, Any]) -> int:
    return (
        int(plan["variant_count"]) * 5
        + int(plan["broadcast_count"]) * 2
        + int(plan["good_detail_pics_count"]) * 2
        + int(plan["required_update_count"]) * 10
    )


def _batch_products(product_plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for product in product_plans:
        grouped[product["complexity_tier"]].append(product)
    batches: list[dict[str, Any]] = []
    global_sequence = 0
    for tier in sorted(grouped, key=TIER_ORDER.__getitem__):
        rows = sorted(grouped[tier], key=lambda row: (row["complexity_score"], row["product_number"]))
        size = TIER_BATCH_SIZE[tier]
        for offset in range(0, len(rows), size):
            chunk = rows[offset : offset + size]
            global_sequence += 1
            batch_id = f"INIT-{global_sequence:03d}-{tier}"
            for position, row in enumerate(chunk, start=1):
                row["batch_id"] = batch_id
                row["batch_sequence"] = global_sequence
                row["position_in_batch"] = position
            batches.append({
                "sequence": global_sequence,
                "batch_id": batch_id,
                "complexity_tier": tier,
                "maximum_product_count": size,
                "product_count": len(chunk),
                "variant_count": sum(row["variant_count"] for row in chunk),
                "estimated_cos_resource_count": sum(row["cos_resource_count"] for row in chunk),
                "required_create_count": len(chunk),
                "required_staged_update_count": sum(row["required_update_count"] for row in chunk),
                "estimated_readback_count": sum(row["estimated_readback_count"] for row in chunk),
                "product_numbers": [row["product_number"] for row in chunk],
                "failure_isolation": {
                    "on_failure": "FREEZE_CURRENT_BATCH_AND_PAUSE_AT_BATCH_BOUNDARY",
                    "subsequent_batches_remain_unchanged": True,
                    "completed_prior_batches_remain_committed": True,
                    "automatic_retry": False,
                },
            })
    return batches


def _pilot_archetype_rank(archetype: str, row: dict[str, Any]) -> tuple[Any, ...]:
    variants = row["variant_count"]
    images = row["broadcast_count"]
    details = row["good_detail_pics_count"]
    dimensions = row["color_count"] * row["size_count"]
    if archetype == "simple":
        return (variants, images, details, row["product_number"])
    if archetype == "multi_sku":
        return (-variants, images, row["product_number"])
    if archetype == "rich_media":
        return (-(images + details), -details, row["product_number"])
    if archetype == "multi_color_size":
        return (-dimensions, -variants, images, row["product_number"])
    return (abs(variants - 6), abs(images - 14), abs(details - 8), row["product_number"])


def select_pilot_20(product_plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in product_plans:
        if row["classification"] in {"footwear", "apparel", "baby", "goods"}:
            by_category[row["classification"]].append(row)
    selected: list[dict[str, Any]] = []
    archetypes = ("simple", "multi_sku", "rich_media", "multi_color_size", "balanced")
    for category in ("footwear", "apparel", "baby", "goods"):
        candidates = by_category[category]
        if len(candidates) < len(archetypes):
            raise InitializationPlanError(f"not enough initialization candidates for pilot category: {category}")
        used: set[str] = set()
        for archetype in archetypes:
            remaining = [row for row in candidates if row["product_number"] not in used]
            choice = min(remaining, key=lambda row: _pilot_archetype_rank(archetype, row))
            used.add(choice["product_number"])
            selected.append({
                "sequence": len(selected) + 1,
                "pilot_category": category,
                "pilot_archetype": archetype,
                **copy.deepcopy(choice),
            })
    if len(selected) != NEW_PILOT_PRODUCT_COUNT or len({row["product_number"] for row in selected}) != NEW_PILOT_PRODUCT_COUNT:
        raise InitializationPlanError("pilot selection is not exactly 20 unique products")
    return selected


def _validate_top_level_inputs(
    stable_catalog: dict[str, Any], source_snapshot: dict[str, Any], special: set[str], category: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if len(special) != EXPECTED_SPECIAL_COUNT:
        raise InitializationPlanError(f"special manifest count must be 351, got {len(special)}")
    if stable_catalog.get("catalog_kind") != "MIKIHOUSE_STABLE_REGULAR_PRODUCT_POOL":
        raise InitializationPlanError("initialization source must be stable_catalog")
    if category.get("id") != TARGET_CATEGORY_ID:
        raise InitializationPlanError("fixed Shijiu MikiHouse category must be 294884")
    source_products = validate_complete_snapshot(source_snapshot)
    stable_products = [row for row in stable_catalog.get("products") or [] if row.get("active", True)]
    declared_count = (stable_catalog.get("stability_audit_summary") or {}).get(
        "stable_catalog_product_count"
    )
    if declared_count is not None and int(declared_count) != len(stable_products):
        raise InitializationPlanError(
            "stable catalog product count does not match its completed audit summary"
        )
    stable_numbers = {str(row.get("product_number")) for row in stable_products}
    if len(stable_numbers) != len(stable_products) or stable_numbers & special:
        raise InitializationPlanError("stable catalog contains duplicate or PDF-special product numbers")
    non_sellable_leaks = sorted(stable_numbers & set(PERMANENT_NON_SELLABLE_PRODUCT_NUMBERS))
    if non_sellable_leaks:
        raise InitializationPlanError(
            f"stable catalog contains permanent non-sellable products: {non_sellable_leaks}"
        )
    source_by_number = {str(row.get("product_number")): row for row in source_products}
    if not stable_numbers <= set(source_by_number):
        raise InitializationPlanError("stable catalog references a product absent from the complete source snapshot")
    for product in stable_products:
        decision = assess_product_stability(product, special)
        if decision.get("status") != STABLE:
            raise InitializationPlanError(
                f"non-stable product leaked into initialization source: {product.get('product_number')}"
            )
        number = str(product.get("product_number"))
        if _source_product_fingerprint(product) != _source_product_fingerprint(source_by_number[number]):
            raise InitializationPlanError(
                f"stable/source product state mismatch requires a stable catalog rebuild: {number}"
            )
    return stable_products, source_products


def build_initialization_plans(
    root: Path,
    stable_catalog: dict[str, Any],
    source_snapshot: dict[str, Any],
    special: set[str],
    mapping: dict[str, Any],
    category: dict[str, Any],
    price_guard: dict[str, Any],
    richtext_contract: dict[str, Any],
    duplicate_name_identity_contract: dict[str, Any],
    duplicate_name_readonly_validation: dict[str, Any] | None,
    *,
    sellable_review_audit: dict[str, Any] | None = None,
    generated_at: str,
    stable_file_sha256: str,
    source_file_sha256: str,
) -> dict[str, Any]:
    stable_products, source_products = _validate_top_level_inputs(
        stable_catalog, source_snapshot, special, category
    )
    source_by_number = {str(row["product_number"]): row for row in source_products}
    names = Counter(str(row.get("name") or "").strip() for row in stable_products)
    all_skus = Counter(
        str(variant.get("sku") or "").strip()
        for product in stable_products
        for variant in product.get("variants") or []
        if variant.get("active", True)
    )
    historical, historical_sources = historical_frozen_product_numbers(root)
    old_plan = validate_historical_plan_is_permanently_stale(root)
    historical.update(old_plan["product_numbers"])
    if price_guard.get("source") != SOURCE or price_guard.get("target") != TARGET:
        raise InitializationPlanError("price guard is not the MIKIHOUSE to SHIJIU contract")
    minimum_price = int(price_guard["minimum_tax_included_price_jpy"])
    maximum_price = int(price_guard["maximum_tax_included_price_jpy"])
    if minimum_price <= 0 or maximum_price < minimum_price:
        raise InitializationPlanError("invalid initialization price guard range")
    if (
        richtext_contract.get("target") != TARGET
        or (richtext_contract.get("good_details") or {}).get("semantics") != "text_or_light_html"
    ):
        raise InitializationPlanError("invalid Shijiu richtext contract")
    if (
        duplicate_name_identity_contract.get("source") != SOURCE
        or duplicate_name_identity_contract.get("target") != TARGET
        or duplicate_name_identity_contract.get("target_category_id") != TARGET_CATEGORY_ID
        or duplicate_name_identity_contract.get("fail_closed") is not True
    ):
        raise InitializationPlanError("invalid duplicate good_name identity contract")
    if duplicate_name_readonly_validation is not None:
        validation_safety = duplicate_name_readonly_validation.get("safety") or {}
        if (
            duplicate_name_readonly_validation.get("status")
            != "READ_ONLY_DUPLICATE_GOOD_NAME_VALIDATION_PASSED"
            or duplicate_name_readonly_validation.get("unexpected_outcome_count") != 0
            or validation_safety.get("shijiu_create_requests") != 0
            or validation_safety.get("shijiu_update_requests") != 0
            or validation_safety.get("shijiu_cos_upload_requests") != 0
            or validation_safety.get("writer_mutex_evidence_generated") is not False
        ):
            raise InitializationPlanError("duplicate good_name read-only evidence is not safe/passed")
    if sellable_review_audit is not None:
        sellable_safety = sellable_review_audit.get("safety") or {}
        high_price_evidence = sellable_review_audit.get("high_price_verification") or {}
        resource_evidence = sellable_review_audit.get("verified_https_resource_closure") or {}
        if (
            sellable_review_audit.get("status")
            != "COMPLETED_OFFICIAL_READ_ONLY_STABLE_POOL_CLOSURE"
            or sellable_review_audit.get("non_sellable_service_or_addon_count") != 27
            or sellable_review_audit.get("remaining_review_required_count") != 0
            or sellable_review_audit.get("stable_non_sellable_leak_product_numbers") != []
            or high_price_evidence.get("verified_real_high_price_product") is not True
            or high_price_evidence.get("guard_changed_this_round") is not True
            or high_price_evidence.get("released_from_initialization_review") is not True
            or high_price_evidence.get("price_change_guards_unchanged") is not True
            or high_price_evidence.get("source_price_upper_bound_current_jpy") != 2_000_000
            or resource_evidence.get("status")
            != "VERIFIED_HTTPS_EQUIVALENT_APPLIED_TO_COMPLETE_CRAWL"
            or resource_evidence.get("entered_stable_sellable_catalog") is not True
            or sellable_safety.get("shijiu_create_requests") != 0
            or sellable_safety.get("shijiu_update_requests") != 0
            or sellable_safety.get("shijiu_cos_upload_requests") != 0
            or sellable_safety.get("writer_mutex_evidence_generated") is not False
        ):
            raise InitializationPlanError("sellable review audit is not safe/passed")
    duplicate_name_audit = analyze_duplicate_names(stable_catalog)
    price_range_audit = audit_price_outside_configured_range(
        stable_catalog,
        minimum_tax_included_price_jpy=minimum_price,
        maximum_tax_included_price_jpy=maximum_price,
    )

    product_plans: list[dict[str, Any]] = []
    disposition_rows: list[dict[str, Any]] = []
    review_rows: list[dict[str, Any]] = []
    mapped_rows: list[dict[str, Any]] = []
    frozen_rows: list[dict[str, Any]] = []
    price_values: list[int] = []
    variant_counts: list[int] = []
    broadcast_counts: list[int] = []
    detail_counts: list[int] = []
    issue_counts: Counter[str] = Counter()
    classifications: Counter[str] = Counter()

    for product in sorted(stable_products, key=lambda row: row["product_number"]):
        number = str(product["product_number"])
        variants = [row for row in product.get("variants") or [] if row.get("active", True)]
        variant_counts.append(len(variants))
        role_counts = _image_role_counts(product)
        broadcast_counts.append(len(product.get("ordered_images") or []))
        detail_counts.append(role_counts["product_gallery"] + role_counts["detail"])
        price_values.extend(int(row["tax_included_price_jpy"]) for row in variants if row.get("tax_included_price_jpy") is not None)
        classification = _classification_name(product)
        classifications[classification] += 1
        issues = _quality_issues(
            product,
            special=special,
            global_sku_counts=all_skus,
            minimum_price=minimum_price,
            maximum_price=maximum_price,
            richtext_contract=richtext_contract,
        )
        mapping_row = (mapping.get("products") or {}).get(number) or {}
        bound = mapping_row.get("shijiu_product_id") not in (None, "")
        base_disposition = {
            "product_number": number,
            "name": product.get("name") or "",
            "classification": classification,
            "variant_count": len(variants),
            "broadcast_count": len(product.get("ordered_images") or []),
            "good_detail_pics_count": role_counts["product_gallery"] + role_counts["detail"],
            "source_content_sha256": product.get("source_content_sha256"),
        }
        if issues:
            for issue in issues:
                issue_counts[issue["code"]] += 1
            row = {**base_disposition, "disposition": "INITIALIZATION_REVIEW_REQUIRED", "issues": issues}
            review_rows.append(row)
            disposition_rows.append(row)
            continue
        if bound:
            row = {
                **base_disposition,
                "disposition": "ALREADY_MAPPED_HANDOFF_TO_INCREMENTAL",
                "shijiu_product_id": str(mapping_row["shijiu_product_id"]),
                "create_allowed": False,
            }
            mapped_rows.append(row)
            disposition_rows.append(row)
            continue
        if number in historical:
            row = {
                **base_disposition,
                "disposition": "HISTORICAL_ATTEMPT_OR_FROZEN_REVIEW_REQUIRED",
                "create_allowed": False,
            }
            frozen_rows.append(row)
            disposition_rows.append(row)
            continue
        item = map_product_to_shijiu(product, category, excluded_product_numbers=special)
        if not item.get("publish_ready"):
            row = {
                **base_disposition,
                "disposition": "INITIALIZATION_REVIEW_REQUIRED",
                "issues": [{"code": "MAPPER_NOT_PUBLISH_READY", "detail": item.get("publish_blockers") or []}],
            }
            review_rows.append(row)
            disposition_rows.append(row)
            issue_counts["MAPPER_NOT_PUBLISH_READY"] += 1
            continue
        plan = _product_stage_plan(product, item, source_by_number[number])
        plan["complexity_tier"] = _complexity_tier(plan)
        plan["complexity_score"] = _complexity_score(plan)
        plan["disposition"] = "PLANNED_INITIAL_CREATE"
        product_plans.append(plan)
        disposition_rows.append({**base_disposition, "disposition": "PLANNED_INITIAL_CREATE"})

    planned_numbers = {row["product_number"] for row in product_plans}
    stable_by_number = {str(row["product_number"]): row for row in stable_products}
    stability_exclusion = stable_catalog.get("stability_exclusion") or {}
    planning_leak_audit = {
        "pdf_special_product_numbers": sorted(planned_numbers & special),
        "web_exclusive_product_numbers": sorted(
            planned_numbers
            & set(stability_exclusion.get("web_exclusive_product_numbers") or [])
        ),
        "limited_time_price_product_numbers": sorted(
            planned_numbers
            & set(stability_exclusion.get("limited_time_price_product_numbers") or [])
        ),
        "review_required_stability_product_numbers": sorted(
            planned_numbers
            & set(stability_exclusion.get("review_required_product_numbers") or [])
        ),
        "non_sellable_service_or_addon_product_numbers": sorted(
            planned_numbers & set(PERMANENT_NON_SELLABLE_PRODUCT_NUMBERS)
        ),
        "zero_or_invalid_price_product_numbers": sorted(
            number
            for number in planned_numbers
            if any(
                int(variant.get("tax_included_price_jpy") or 0) <= 0
                for variant in stable_by_number[number].get("variants") or []
                if variant.get("active", True)
            )
        ),
        "missing_required_media_product_numbers": sorted(
            number
            for number in planned_numbers
            if (
                not str(
                    (stable_by_number[number].get("main_image") or {}).get("url") or ""
                ).strip()
                or not (stable_by_number[number].get("ordered_images") or [])
                or any(
                    not str((variant.get("resolved_image") or {}).get("url") or "").strip()
                    for variant in stable_by_number[number].get("variants") or []
                    if variant.get("active", True)
                )
            )
        ),
    }
    planning_leak_audit["all_forbidden_leak_product_numbers"] = sorted({
        number
        for key, numbers in planning_leak_audit.items()
        if key.endswith("_product_numbers")
        for number in numbers
    })
    planning_leak_audit["passed"] = not planning_leak_audit[
        "all_forbidden_leak_product_numbers"
    ]
    if not planning_leak_audit["passed"]:
        raise InitializationPlanError(
            "forbidden or non-sellable product leaked into initialization plan: "
            f"{planning_leak_audit['all_forbidden_leak_product_numbers']}"
        )

    batches = _batch_products(product_plans)
    pilot_products = select_pilot_20(product_plans)
    logical_stable_hash = content_sha256(stable_catalog)
    logical_source_hash = content_sha256(source_snapshot)
    freshness = {
        "stable_catalog_file_sha256": stable_file_sha256,
        "stable_catalog_logical_sha256": logical_stable_hash,
        "source_snapshot_file_sha256": source_file_sha256,
        "source_snapshot_logical_sha256": logical_source_hash,
        "source_snapshot_captured_at": source_snapshot.get("captured_at"),
        "stable_catalog_synced_at": stable_catalog.get("synced_at"),
        "requires_new_complete_storefront_crawl_after_generated_at": True,
        "fail_closed_if_any_top_level_hash_changes": True,
        "per_product_regeneration_required_for_price_stock_variant_or_image_change": True,
        "stability_reclassification_before_any_target_mutation": True,
        "richtext_contract_logical_sha256": content_sha256(richtext_contract),
        "duplicate_name_identity_contract_logical_sha256": content_sha256(
            duplicate_name_identity_contract
        ),
    }
    common_safety = {
        "mode": PLANNING_STATUS,
        "write_status": WRITE_BLOCKED_STATUS,
        "execution_authorized": False,
        "shijiu_requests": 0,
        "shijiu_create_requests": 0,
        "shijiu_update_requests": 0,
        "shijiu_cos_upload_requests": 0,
        "shijiu_shelf_price_inventory_writes": 0,
        "writer_mutex_evidence_generated": False,
        "writer_mutex_evidence_generation_allowed_this_round": False,
        "official_image_download_count": 0,
        "legacy_286_touched": False,
    }
    batch_plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "generated_at": generated_at,
        "status": PLANNING_STATUS,
        "write_status": WRITE_BLOCKED_STATUS,
        "source": SOURCE,
        "target": TARGET,
        "fixed_target_category_id": TARGET_CATEGORY_ID,
        "source_of_truth": "stable_catalog",
        "execution_authorized": False,
        "freshness_guard": freshness,
        "identity": {
            "product": "product_number",
            "variant": "product_number::variant SKU",
            "target_variant": "shijiu_product_id + exact backend_sku_code",
            "shijiu_sku_id": "nullable",
        },
        "counts": {
            "stable_catalog_product_count": len(stable_products),
            "planned_initial_create_product_count": len(product_plans),
            "already_mapped_handoff_count": len(mapped_rows),
            "historical_frozen_count": len(frozen_rows),
            "initialization_review_required_count": len(review_rows),
            "accounted_product_count": len(disposition_rows),
            "batch_count": len(batches),
        },
        "batch_policy": {
            "tier_order": list(TIER_ORDER),
            "maximum_products_per_tier_batch": TIER_BATCH_SIZE,
            "one_batch_failure_never_mutates_later_batch_checkpoints": True,
            "automatic_mutation_retry_count": 0,
            "product_stage_checkpoint_required": True,
        },
        "batches": batches,
        "products": product_plans,
        "non_create_dispositions": {
            "already_mapped_handoff": mapped_rows,
            "historical_attempt_or_frozen": frozen_rows,
            "initialization_review_required": review_rows,
        },
        "all_product_dispositions": disposition_rows,
        "safety": common_safety,
    }
    pilot_plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "generated_at": generated_at,
        "status": "FROZEN_PLANNING_ONLY",
        "write_status": WRITE_BLOCKED_STATUS,
        "source": SOURCE,
        "target": TARGET,
        "fixed_target_category_id": TARGET_CATEGORY_ID,
        "product_count": len(pilot_products),
        "coverage": dict(sorted(Counter(row["pilot_category"] for row in pilot_products).items())),
        "freshness_guard": freshness,
        "selection_policy": (
            "exactly five deterministic representatives per footwear/apparel/baby/goods; "
            "stable-only, publishable, unmapped, non-special, non-historical; duplicate names "
            "use exact-name candidate scope plus complete SKU-set strong identity; "
            "cover simple/multi-SKU/rich-media/multi-color-size/balanced archetypes"
        ),
        "products": pilot_products,
        "historical_plan": old_plan,
        "execution_authorized": False,
        "must_refresh_and_rebuild_before_future_execution": True,
        "safety": common_safety,
    }
    quality_audit = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "generated_at": generated_at,
        "status": "COMPLETED_OFFLINE_DATA_QUALITY_AUDIT",
        "mode": PLANNING_STATUS,
        "write_status": WRITE_BLOCKED_STATUS,
        "source": SOURCE,
        "stable_catalog_product_count": len(stable_products),
        "stable_catalog_variant_count": sum(variant_counts),
        "stable_catalog_image_resource_count": sum(broadcast_counts),
        "price_jpy_distribution": _percentiles(price_values),
        "price_outside_configured_range_product_count": sum(
            any(issue["code"] == "PRICE_OUTSIDE_CONFIGURED_RANGE" for issue in row.get("issues") or [])
            for row in review_rows
        ),
        "variant_count_distribution": _distribution(variant_counts),
        "variant_count_statistics": _percentiles(variant_counts),
        "maximum_variant_products": [
            row["product_number"]
            for row in stable_products
            if len([variant for variant in row.get("variants") or [] if variant.get("active", True)])
            == max(variant_counts, default=0)
        ],
        "broadcast_count_distribution": _distribution(broadcast_counts),
        "broadcast_count_statistics": _percentiles(broadcast_counts),
        "maximum_broadcast_products": [
            row["product_number"]
            for row in stable_products
            if len(row.get("ordered_images") or []) == max(broadcast_counts, default=0)
        ],
        "good_detail_pics_count_distribution": _distribution(detail_counts),
        "good_detail_pics_count_statistics": _percentiles(detail_counts),
        "maximum_good_detail_pics_products": [
            row["product_number"]
            for row in stable_products
            if sum(
                1
                for image in row.get("ordered_images") or []
                if image.get("role") in {"product_gallery", "detail"}
            ) == max(detail_counts, default=0)
        ],
        "classification_counts": dict(sorted(classifications.items())),
        "sellable_quality_policy": {
            "positive_tax_included_price_required": True,
            "active_variant_required": True,
            "valid_variant_sku_required": True,
            "main_ordered_and_variant_media_required": True,
            "non_sellable_service_or_addon_forbidden": True,
            "non_sellable_permanent_manifest_count": len(
                PERMANENT_NON_SELLABLE_PRODUCT_NUMBERS
            ),
            "non_sellable_leaks_in_initialization_plan": planning_leak_audit[
                "non_sellable_service_or_addon_product_numbers"
            ],
        },
        "initialization_plan_leak_audit": planning_leak_audit,
        "quality_issue_counts": dict(sorted(issue_counts.items())),
        "duplicate_name_groups": [
            {"name": name, "count": count}
            for name, count in sorted(names.items()) if name and count > 1
        ],
        "duplicate_good_name_identity_audit": {
            key: duplicate_name_audit[key]
            for key in (
                "duplicate_name_group_count",
                "duplicate_name_product_count",
                "group_size_distribution",
                "maximum_group_size",
                "maximum_groups",
                "globally_duplicated_backend_sku_code_count",
                "identical_complete_backend_sku_set_group_count",
                "all_duplicate_name_products_have_source_unique_complete_sku_sets",
                "theoretical_duplicate_name_review_release_count",
            )
        },
        "review_required_products": review_rows,
        "missing_image_product_count": sum(
            any(issue["code"] in {"MISSING_MAIN_IMAGE", "MISSING_ORDERED_IMAGE", "MISSING_VARIANT_IMAGE"} for issue in row.get("issues") or [])
            for row in review_rows
        ),
        "missing_sku_product_count": sum(
            any(issue["code"] == "MISSING_VARIANT_SKU" for issue in row.get("issues") or [])
            for row in review_rows
        ),
        "variant_identity_anomaly_product_count": sum(
            any(issue["code"] in {
                "MISSING_VARIANT_SKU", "DUPLICATE_VARIANT_SKU_WITHIN_PRODUCT",
                "DUPLICATE_VARIANT_SKU_ACROSS_PRODUCTS", "INVALID_VARIANT_STABLE_ID",
            } for issue in row.get("issues") or [])
            for row in review_rows
        ),
        "all_stable_products_accounted": len(disposition_rows) == len(stable_products),
        "planning_interpretation": (
            "All stable products are accounted for, but only data-quality-clean, unmapped, "
            "non-historical products receive executable initialization stages. REVIEW_REQUIRED "
            "products remain outside every batch until their recorded anomaly is resolved."
        ),
        "safety": common_safety,
    }
    total_stages = sum(len(row["stages"]) for row in product_plans)
    capacity = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "generated_at": generated_at,
        "status": PLANNING_STATUS,
        "write_status": WRITE_BLOCKED_STATUS,
        "planned_product_count": len(product_plans),
        "planned_variant_count": sum(row["variant_count"] for row in product_plans),
        "stable_pool_product_count": len(stable_products),
        "stable_pool_variant_count": sum(variant_counts),
        "stable_pool_source_image_resource_count": sum(broadcast_counts),
        "non_create_review_required_product_count": len(review_rows),
        "non_create_historical_frozen_product_count": len(frozen_rows),
        "non_create_already_mapped_product_count": len(mapped_rows),
        "estimated_create_count": len(product_plans),
        "estimated_staged_update_count": sum(row["required_update_count"] for row in product_plans),
        "estimated_total_mutation_count": total_stages,
        "estimated_unique_cos_resource_upload_count": sum(row["cos_resource_count"] for row in product_plans),
        "estimated_strong_readback_count": sum(row["estimated_readback_count"] for row in product_plans),
        "batch_count": len(batches),
        "batch_workload": batches,
        "estimation_notes": [
            "COS count is unique ordered source resources per planned product; no image was downloaded or uploaded.",
            "Mutation and COS estimates exclude REVIEW_REQUIRED, historical-frozen, and already-mapped products.",
            "Each CREATE/UPDATE stage budgets Goods.index plus getFormatInfo strong readback.",
            "Actual future counts must be regenerated after the mandatory fresh full crawl and resource preflight.",
        ],
        "safety": common_safety,
    }
    readiness = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "generated_at": generated_at,
        "status": "INITIALIZATION_PLANS_READY_OFFLINE_EXECUTION_BLOCKED",
        "mode": PLANNING_STATUS,
        "write_status": WRITE_BLOCKED_STATUS,
        "source": SOURCE,
        "target": TARGET,
        "stable_catalog_is_only_source_of_truth": True,
        "freshness_guard": freshness,
        "new_pilot_plan": {
            "product_count": len(pilot_products),
            "coverage": pilot_plan["coverage"],
            "execution_authorized": False,
        },
        "full_initialization": batch_plan["counts"],
        "handoff_contract": {
            "after_verified_mapping": "mark INITIALIZED_HANDOFF_INCREMENTAL in initialization checkpoint and source sync state",
            "future_create_for_mapped_product": "FORBIDDEN",
            "subsequent_maintenance_events": [
                "PRICE_CHANGED", "INVENTORY_CHANGED", "IMAGE_CHANGED", "PRODUCT_INACTIVE",
                "VARIANT_INACTIVE", "PRODUCT_REACTIVATED", "VARIANT_REACTIVATED",
                "STABILITY_QUARANTINE", "STABILITY_RESTORED",
            ],
        },
        "historical_prohibited_product_count": len(historical),
        "historical_prohibited_sources": historical_sources,
        "historical_plan": old_plan,
        "duplicate_good_name_identity": {
            "contract_logical_sha256": content_sha256(duplicate_name_identity_contract),
            "source_duplicate_name_group_count": duplicate_name_audit["duplicate_name_group_count"],
            "source_duplicate_name_product_count": duplicate_name_audit["duplicate_name_product_count"],
            "source_unique_complete_sku_sets": duplicate_name_audit[
                "all_duplicate_name_products_have_source_unique_complete_sku_sets"
            ],
            "create_readback_outcome_required": "UNIQUE_STRONG_MATCH",
            "shijiu_readonly_validation": (
                {
                    "status": duplicate_name_readonly_validation["status"],
                    "selected_group_count": duplicate_name_readonly_validation[
                        "selected_group_count"
                    ],
                    "mapped_unique_strong_match_count": duplicate_name_readonly_validation[
                        "mapped_unique_strong_match_count"
                    ],
                    "unmapped_not_found_count": duplicate_name_readonly_validation[
                        "unmapped_not_found_count"
                    ],
                    "shijiu_read_request_count": duplicate_name_readonly_validation["safety"][
                        "shijiu_read_requests"
                    ],
                    "evidence_logical_sha256": content_sha256(
                        duplicate_name_readonly_validation
                    ),
                }
                if duplicate_name_readonly_validation is not None
                else {"status": "NOT_CAPTURED"}
            ),
        },
        "sellable_review_resolution": (
            {
                "status": sellable_review_audit["status"],
                "official_page_audit_product_count": sellable_review_audit[
                    "official_page_audit_product_count"
                ],
                "non_sellable_service_or_addon_count": sellable_review_audit[
                    "non_sellable_service_or_addon_count"
                ],
                "remaining_review_required_count": sellable_review_audit[
                    "remaining_review_required_count"
                ],
                "remaining_review_required_product_numbers": sellable_review_audit[
                    "remaining_review_required_product_numbers"
                ],
                "high_price_guard_changed": sellable_review_audit[
                    "high_price_verification"
                ]["guard_changed_this_round"],
                "evidence_logical_sha256": content_sha256(sellable_review_audit),
            }
            if sellable_review_audit is not None
            else {"status": "NOT_CAPTURED"}
        ),
        "initialization_plan_leak_audit": planning_leak_audit,
        "execution_prerequisites_not_satisfied_this_round": [
            "WAWU or any other writer must be confirmed stopped in a future task",
            "new complete Storefront crawl and plan freshness validation",
            "per-product immediate resource preflight",
            "valid external writer mutex evidence and local global mutex",
            "separate explicit live-write authorization",
        ],
        "new_20_plan_execution_count": 0,
        "full_batch_execution_count": 0,
        "safety": common_safety,
    }
    sellable_resolution_summary = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "generated_at": generated_at,
        "status": "COMPLETED_PLANNING_ONLY_SELLABLE_INITIALIZATION_RESOLUTION",
        "mode": PLANNING_STATUS,
        "write_status": WRITE_BLOCKED_STATUS,
        "source": SOURCE,
        "target": TARGET,
        "storefront_total_product_count": (
            (stable_catalog.get("stability_audit_summary") or {}).get(
                "storefront_total_product_count"
            )
        ),
        "final_sellable_stable_product_count": len(stable_products),
        "non_sellable_service_or_addon": {
            "count": len(PERMANENT_NON_SELLABLE_PRODUCT_NUMBERS),
            "product_numbers": sorted(PERMANENT_NON_SELLABLE_PRODUCT_NUMBERS),
            "permanently_forbidden_actions": [
                "CREATE",
                "PRICE_CHANGED",
                "INVENTORY_CHANGED",
                "IMAGE_CHANGED",
                "PRODUCT_REACTIVATED",
                "VARIANT_REACTIVATED",
            ],
        },
        "remaining_initialization_review_required": {
            "count": len(review_rows),
            "products": review_rows,
        },
        "separate_stability_review_outside_sellable_catalog": {
            "count": len(stability_exclusion.get("review_required_product_numbers") or []),
            "product_numbers": sorted(
                stability_exclusion.get("review_required_product_numbers") or []
            ),
        },
        "initialization_counts": batch_plan["counts"],
        "pilot_20": {
            "product_count": len(pilot_products),
            "coverage": pilot_plan["coverage"],
            "all_still_valid": len(pilot_products) == NEW_PILOT_PRODUCT_COUNT,
            "execution_count": 0,
        },
        "initialization_plan_leak_audit": planning_leak_audit,
        "high_price_verification": (
            sellable_review_audit.get("high_price_verification")
            if sellable_review_audit is not None
            else {"status": "NOT_CAPTURED"}
        ),
        "safety": common_safety,
    }
    return {
        "batch_plan": batch_plan,
        "pilot_plan": pilot_plan,
        "quality_audit": quality_audit,
        "duplicate_name_audit": {
            "schema_version": PLAN_SCHEMA_VERSION,
            "generated_at": generated_at,
            "status": "COMPLETED_OFFLINE_DUPLICATE_GOOD_NAME_AUDIT",
            "mode": PLANNING_STATUS,
            "write_status": WRITE_BLOCKED_STATUS,
            "source": SOURCE,
            "target": TARGET,
            **duplicate_name_audit,
            "identity_contract_logical_sha256": content_sha256(
                duplicate_name_identity_contract
            ),
            "safety": common_safety,
        },
        "price_range_audit": {
            "schema_version": PLAN_SCHEMA_VERSION,
            "generated_at": generated_at,
            "status": (
                "COMPLETED_OFFLINE_PRICE_RANGE_AUDIT_SOURCE_CEILING_UPDATED"
                if price_range_audit.get("guard_changed")
                else "COMPLETED_OFFLINE_PRICE_RANGE_AUDIT_GUARD_UNCHANGED"
            ),
            "mode": PLANNING_STATUS,
            "write_status": WRITE_BLOCKED_STATUS,
            "source": SOURCE,
            "target": TARGET,
            **price_range_audit,
            "safety": common_safety,
        },
        "capacity": capacity,
        "readiness": readiness,
        "sellable_resolution_summary": sellable_resolution_summary,
    }


def validate_plan_freshness(
    plan: dict[str, Any],
    stable_catalog: dict[str, Any],
    source_snapshot: dict[str, Any],
    special: set[str],
    mapping: dict[str, Any],
) -> dict[str, Any]:
    validate_complete_snapshot(source_snapshot)
    freshness = plan.get("freshness_guard") or {}
    reasons: list[dict[str, Any]] = []
    if content_sha256(stable_catalog) != freshness.get("stable_catalog_logical_sha256"):
        reasons.append({"code": "STABLE_CATALOG_HASH_CHANGED"})
    if content_sha256(source_snapshot) != freshness.get("source_snapshot_logical_sha256"):
        reasons.append({"code": "SOURCE_SNAPSHOT_HASH_CHANGED"})
    stable_by_number = {
        str(row.get("product_number")): row
        for row in stable_catalog.get("products") or [] if row.get("active", True)
    }
    source_by_number = {
        str(row.get("product_number")): row for row in source_snapshot.get("products") or []
    }
    stale_products = []
    for planned in plan.get("products") or []:
        number = str(planned.get("product_number") or "")
        current = stable_by_number.get(number)
        source = source_by_number.get(number)
        product_reasons = []
        if number in special:
            product_reasons.append(PDF_SPECIAL)
        if not current or assess_product_stability(current or {}, special).get("status") != STABLE:
            product_reasons.append("NO_LONGER_STABLE_OR_ACTIVE")
        if current and current.get("source_content_sha256") != planned.get("source_content_sha256"):
            product_reasons.append("STABLE_PRODUCT_STATE_CHANGED")
        if not source or _source_product_fingerprint(source) != planned.get("source_snapshot_product_fingerprint_sha256"):
            product_reasons.append("SOURCE_PRODUCT_STATE_CHANGED")
        mapping_row = (mapping.get("products") or {}).get(number) or {}
        if mapping_row.get("shijiu_product_id") not in (None, ""):
            product_reasons.append("ALREADY_MAPPED_CREATE_FORBIDDEN")
        if product_reasons:
            stale_products.append({"product_number": number, "reasons": product_reasons})
    if stale_products:
        reasons.append({"code": "PRODUCT_FRESHNESS_FAILED", "products": stale_products})
    return {
        "valid": not reasons,
        "status": "FRESH" if not reasons else "STALE_FAIL_CLOSED_REBUILD_REQUIRED",
        "reasons": reasons,
        "target_requests": 0,
        "mutation_allowed": False,
    }


def initialize_checkpoint(plan: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    plan_hash = content_sha256(plan)
    if existing is not None:
        if existing.get("plan_sha256") != plan_hash:
            raise InitializationPlanError("existing initialization checkpoint belongs to a different plan")
        return copy.deepcopy(existing)
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "created_at": plan.get("generated_at"),
        "updated_at": plan.get("generated_at"),
        "status": "FROZEN_PLANNING_ONLY",
        "write_status": WRITE_BLOCKED_STATUS,
        "source": SOURCE,
        "target": TARGET,
        "plan_sha256": plan_hash,
        "batches": {
            row["batch_id"]: {
                "sequence": row["sequence"],
                "status": "PLANNED",
                "product_numbers": list(row["product_numbers"]),
                "error_sha256": None,
            }
            for row in plan.get("batches") or []
        },
        "products": {
            row["product_number"]: {
                "batch_id": row["batch_id"],
                "status": "PLANNED",
                "completed_stage_keys": [],
                "shijiu_product_id": None,
                "mapping_verified": False,
            }
            for row in plan.get("products") or []
        },
        "initialization_handoff_count": 0,
        "shijiu_mutation_count": 0,
        "writer_mutex_evidence_generated": False,
    }


def freeze_initialization_batch(
    checkpoint: dict[str, Any], batch_id: str, error_summary: str
) -> dict[str, Any]:
    if batch_id not in checkpoint.get("batches", {}):
        raise InitializationPlanError(f"unknown initialization batch: {batch_id}")
    result = copy.deepcopy(checkpoint)
    batch = result["batches"][batch_id]
    batch["status"] = "FROZEN_FAILED"
    batch["error_sha256"] = hashlib.sha256(error_summary.encode("utf-8")).hexdigest()
    for number in batch["product_numbers"]:
        if result["products"][number]["status"] == "PLANNED":
            result["products"][number]["status"] = "FROZEN_WITH_BATCH"
    result["status"] = "PAUSED_CURRENT_BATCH_FROZEN_LATER_BATCHES_UNCHANGED"
    return result


def handoff_initialized_product(
    checkpoint: dict[str, Any],
    source_sync_state: dict[str, Any],
    mapping: dict[str, Any],
    product_number: str,
    verified_at: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    product_checkpoint = (checkpoint.get("products") or {}).get(product_number)
    mapping_row = (mapping.get("products") or {}).get(product_number) or {}
    source_row = (source_sync_state.get("products") or {}).get(product_number) or {}
    if not product_checkpoint:
        raise InitializationPlanError("product is not in the initialization plan")
    if mapping_row.get("source") != SOURCE or mapping_row.get("shijiu_product_id") in (None, ""):
        raise InitializationPlanError("verified MIKIHOUSE mapping is required before handoff")
    if source_row.get("stability_status") != STABLE or source_row.get("source_presence") != "ACTIVE":
        raise InitializationPlanError("only an active stable source product can enter incremental handoff")
    new_checkpoint = copy.deepcopy(checkpoint)
    new_source_state = copy.deepcopy(source_sync_state)
    row = new_checkpoint["products"][product_number]
    if row.get("status") == "INITIALIZED_HANDOFF_INCREMENTAL":
        return new_checkpoint, new_source_state
    row.update({
        "status": "INITIALIZED_HANDOFF_INCREMENTAL",
        "shijiu_product_id": str(mapping_row["shijiu_product_id"]),
        "mapping_verified": True,
        "handoff_verified_at": verified_at,
    })
    new_checkpoint["initialization_handoff_count"] = sum(
        item.get("status") == "INITIALIZED_HANDOFF_INCREMENTAL"
        for item in new_checkpoint["products"].values()
    )
    new_source_state.setdefault("initialization_handoffs", {})[product_number] = {
        "source": SOURCE,
        "shijiu_product_id": str(mapping_row["shijiu_product_id"]),
        "verified_at": verified_at,
        "maintenance_mode": "INCREMENTAL_EVENTS_ONLY_NO_FUTURE_CREATE",
    }
    return new_checkpoint, new_source_state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the complete MIKIHOUSE Shijiu initialization plan without target requests"
    )
    parser.add_argument("--stable", type=Path, default=Path("deliverables/storefront_stable_catalog/stable_catalog.json.gz"))
    parser.add_argument("--source", type=Path, default=Path("output/storefront-stable/source_catalog.json"))
    parser.add_argument("--special", type=Path, default=Path("special_skus_2026aw.csv"))
    parser.add_argument("--mapping", type=Path, default=Path("state/shijiu_mappings.json"))
    parser.add_argument("--category", type=Path, default=Path("config/shijiu_category_map.json"))
    parser.add_argument("--price-guard", type=Path, default=Path("config/shijiu_price_guard.json"))
    parser.add_argument("--richtext-contract", type=Path, default=Path("config/shijiu_richtext_contract.json"))
    parser.add_argument(
        "--duplicate-name-identity-contract",
        type=Path,
        default=Path("config/shijiu_duplicate_good_name_identity_contract.json"),
    )
    parser.add_argument(
        "--duplicate-name-readonly-validation",
        type=Path,
        default=Path(
            "deliverables/shijiu_initialization/"
            "duplicate_good_name_shijiu_readonly_validation.json"
        ),
    )
    parser.add_argument(
        "--sellable-review-audit",
        type=Path,
        default=Path(
            "deliverables/storefront_stable_catalog/"
            "sellable_review_resolution_audit.json"
        ),
    )
    parser.add_argument("--output", type=Path, default=Path("deliverables/shijiu_initialization"))
    parser.add_argument("--checkpoint", type=Path, default=Path("state/mikihouse_initialization_checkpoint.json.gz"))
    parser.add_argument(
        "--replace-planning-only-checkpoint",
        action="store_true",
        help="replace only a zero-mutation FROZEN_PLANNING_ONLY checkpoint after replanning",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path.cwd()
    stable_catalog = _read_json(args.stable)
    source_snapshot = _read_json(args.source)
    special = set(read_product_numbers(args.special))
    mapping = _read_json(args.mapping)
    category = load_category_map(args.category)
    price_guard = _read_json(args.price_guard)
    richtext_contract = _read_json(args.richtext_contract)
    duplicate_name_identity_contract = _read_json(args.duplicate_name_identity_contract)
    duplicate_name_readonly_validation = (
        _read_json(args.duplicate_name_readonly_validation)
        if args.duplicate_name_readonly_validation.exists()
        else None
    )
    sellable_review_audit = (
        _read_json(args.sellable_review_audit)
        if args.sellable_review_audit.exists()
        else None
    )
    generated_at = str(source_snapshot.get("captured_at"))
    result = build_initialization_plans(
        root,
        stable_catalog,
        source_snapshot,
        special,
        mapping,
        category,
        price_guard,
        richtext_contract,
        duplicate_name_identity_contract,
        duplicate_name_readonly_validation,
        sellable_review_audit=sellable_review_audit,
        generated_at=generated_at,
        stable_file_sha256=_file_sha256(args.stable),
        source_file_sha256=_file_sha256(args.source),
    )
    args.output.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(args.output / "stable_pilot_20_frozen_plan.json", result["pilot_plan"])
    _write_json_atomic(args.output / "stable_initialization_batch_plan.json.gz", result["batch_plan"])
    _write_json_atomic(args.output / "stable_initialization_batch_summary.json", {
        key: result["batch_plan"][key]
        for key in ("schema_version", "generated_at", "status", "write_status", "counts", "batch_policy", "batches", "safety")
    })
    _write_json_atomic(args.output / "stable_initialization_data_quality_audit.json", result["quality_audit"])
    _write_json_atomic(
        args.output / "duplicate_good_name_offline_audit.json",
        result["duplicate_name_audit"],
    )
    _write_json_atomic(
        args.output / "price_outside_configured_range_audit.json",
        result["price_range_audit"],
    )
    _write_json_atomic(args.output / "stable_initialization_capacity_estimate.json", result["capacity"])
    _write_json_atomic(args.output / "stable_initialization_readiness.json", result["readiness"])
    _write_json_atomic(
        args.output / "sellable_initialization_resolution_report.json",
        result["sellable_resolution_summary"],
    )
    checkpoint = initialize_checkpoint(result["batch_plan"])
    if args.checkpoint.exists():
        existing = _read_json(args.checkpoint)
        if existing.get("plan_sha256") == checkpoint["plan_sha256"]:
            checkpoint = initialize_checkpoint(result["batch_plan"], existing)
        elif (
            args.replace_planning_only_checkpoint
            and existing.get("status") == "FROZEN_PLANNING_ONLY"
            and existing.get("shijiu_mutation_count") == 0
            and existing.get("writer_mutex_evidence_generated") is False
        ):
            _write_json_atomic(args.checkpoint, checkpoint)
        else:
            raise InitializationPlanError(
                "initialization checkpoint changed; replacement requires the explicit planning-only "
                "flag and a zero-mutation frozen checkpoint"
            )
    else:
        _write_json_atomic(args.checkpoint, checkpoint)
    print(json.dumps(result["readiness"], ensure_ascii=False, indent=2))
    return 0
