from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .catalog import calculate_mini_program_price_jpy
from .csv_input import read_product_numbers
from .stable_catalog import (
    LIMITED_TIME_PRICE,
    NON_SELLABLE_SERVICE_OR_ADDON,
    PDF_SPECIAL,
    PERMANENT_NON_SELLABLE_PRODUCT_NUMBERS,
    REVIEW_REQUIRED,
    STABLE,
    WEB_EXCLUSIVE,
    assess_product_stability,
    run_stable_catalog_sync,
)


SYNC_STATE_SCHEMA_VERSION = 1
SYNC_EVENT_SCHEMA_VERSION = 1
SOURCE = "MIKIHOUSE"
TARGET = "SHIJIU"
TARGET_CATEGORY_ID = 294884
PLANNING_ONLY = "PLANNING_ONLY"

NEW_PRODUCT = "NEW_PRODUCT"
NEW_VARIANT = "NEW_VARIANT"
PRICE_CHANGED = "PRICE_CHANGED"
INVENTORY_CHANGED = "INVENTORY_CHANGED"
IMAGE_CHANGED = "IMAGE_CHANGED"
PRODUCT_INACTIVE = "PRODUCT_INACTIVE"
VARIANT_INACTIVE = "VARIANT_INACTIVE"
PRODUCT_REACTIVATED = "PRODUCT_REACTIVATED"
VARIANT_REACTIVATED = "VARIANT_REACTIVATED"
STABILITY_QUARANTINE = "STABILITY_QUARANTINE"
STABILITY_RESTORED = "STABILITY_RESTORED"
NO_CHANGE = "NO_CHANGE"
REVIEW_EVENT = "REVIEW_REQUIRED"

ALL_EVENT_TYPES = (
    NEW_PRODUCT,
    NEW_VARIANT,
    PRICE_CHANGED,
    INVENTORY_CHANGED,
    IMAGE_CHANGED,
    PRODUCT_INACTIVE,
    VARIANT_INACTIVE,
    PRODUCT_REACTIVATED,
    VARIANT_REACTIVATED,
    STABILITY_QUARANTINE,
    STABILITY_RESTORED,
    NO_CHANGE,
    REVIEW_EVENT,
)

UNSTABLE_STATUSES = frozenset({
    WEB_EXCLUSIVE,
    LIMITED_TIME_PRICE,
    REVIEW_REQUIRED,
    NON_SELLABLE_SERVICE_OR_ADDON,
})


class SyncCycleError(RuntimeError):
    pass


class IncompleteCrawlError(SyncCycleError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def content_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise SyncCycleError(f"JSON root must be an object: {path}")
    return value


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    if path.suffix == ".gz":
        with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=9) as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
    else:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_price_guard(path: Path) -> dict[str, Any]:
    guard = _read_json(path)
    if guard.get("source") != SOURCE or guard.get("target") != TARGET:
        raise SyncCycleError("price guard must declare MIKIHOUSE -> SHIJIU")
    required = (
        "minimum_tax_included_price_jpy",
        "maximum_tax_included_price_jpy",
        "maximum_absolute_change_jpy",
        "maximum_relative_change_ratio",
    )
    try:
        if any(Decimal(str(guard[key])) < 0 for key in required):
            raise SyncCycleError("price guard thresholds must be non-negative")
    except (KeyError, ValueError) as exc:
        raise SyncCycleError("price guard is incomplete") from exc
    return guard


def validate_complete_snapshot(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    if snapshot.get("catalog_kind") != "MIKIHOUSE_COMPLETE_STOREFRONT_SOURCE_SNAPSHOT":
        raise IncompleteCrawlError("source snapshot has an unexpected catalog_kind")
    if snapshot.get("complete_pagination_validated") is not True:
        raise IncompleteCrawlError("source snapshot pagination is not explicitly complete")
    products = snapshot.get("products")
    if not isinstance(products, list) or not products:
        raise IncompleteCrawlError("complete source snapshot must contain products")
    numbers: set[str] = set()
    stable_variant_ids: set[str] = set()
    for product in products:
        number = str(product.get("product_number") or product.get("handle") or "").strip()
        if not number or number in numbers:
            raise IncompleteCrawlError(f"missing or duplicate product identity: {number!r}")
        numbers.add(number)
        variants = product.get("variants")
        if not isinstance(variants, list) or not variants:
            raise IncompleteCrawlError(f"source product has no variants: {number}")
        local_skus: set[str] = set()
        for variant in variants:
            sku = str(variant.get("sku") or "").strip()
            identity = f"{number}::{sku}"
            if not sku or sku in local_skus or identity in stable_variant_ids:
                raise IncompleteCrawlError(f"missing or duplicate variant identity: {identity}")
            local_skus.add(sku)
            stable_variant_ids.add(identity)
            if "available_for_sale" not in variant:
                raise IncompleteCrawlError(f"variant availability missing: {identity}")
            try:
                tax = int(variant["tax_included_price_jpy"])
            except (KeyError, TypeError, ValueError) as exc:
                raise IncompleteCrawlError(f"variant price invalid: {identity}") from exc
            if tax < 0:
                raise IncompleteCrawlError(f"variant price invalid: {identity}")
    return products


def _stability_status(product: dict[str, Any], special: set[str]) -> tuple[str, list[dict[str, Any]]]:
    decision = assess_product_stability(product, special)
    if decision["status"] == STABLE:
        return STABLE, []
    return str(decision["excluded_reason"]), copy.deepcopy(decision.get("evidence") or [])


def _image_descriptor(product: dict[str, Any]) -> list[dict[str, Any]]:
    resources = []
    seen: set[str] = set()
    for entry in product.get("ordered_images") or []:
        image = entry.get("image") or {}
        url = str(image.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        resources.append({
            "order": len(resources) + 1,
            "role": str(entry.get("role") or "product_gallery"),
            "source_url": url,
            "width": image.get("width"),
            "height": image.get("height"),
            "alt_text": str(image.get("alt_text") or ""),
            "colors": list(entry.get("colors") or []),
            "variant_skus": list(entry.get("variant_skus") or []),
        })
    return resources


def _variant_record(product_number: str, variant: dict[str, Any], status: str) -> dict[str, Any]:
    sku = str(variant["sku"])
    tax = int(variant["tax_included_price_jpy"])
    image = variant.get("variant_image") or variant.get("resolved_image") or {}
    normal_target = calculate_mini_program_price_jpy(tax) if status == STABLE else None
    return {
        "source_variant_id": f"MIKIHOUSE:{product_number}:{sku}",
        "stable_id": str(variant.get("stable_id") or ""),
        "sku": sku,
        "source_presence": "ACTIVE",
        "tax_included_price_jpy": tax,
        "compare_at_price_jpy": variant.get("compare_at_price_jpy"),
        "target_price_jpy": normal_target,
        "available_for_sale": bool(variant["available_for_sale"]),
        "target_stock": 1 if variant["available_for_sale"] else 0,
        "color": str(variant.get("color") or ""),
        "size": str(variant.get("size") or ""),
        "selected_options": copy.deepcopy(variant.get("selected_options") or []),
        "variant_image_url": str(image.get("url") or ""),
        "variant_image_sha256": content_sha256(image),
    }


def build_current_records(
    products: list[dict[str, Any]], special: set[str], captured_at: str
) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for product in products:
        number = str(product.get("product_number") or product.get("handle") or "").strip()
        status, evidence = _stability_status(product, special)
        images = _image_descriptor(product)
        variants = {
            str(row["sku"]): _variant_record(number, row, status)
            for row in product.get("variants") or []
        }
        record = {
            "source": SOURCE,
            "source_product_id": f"MIKIHOUSE:{number}",
            "product_number": number,
            "name": str(product.get("name") or ""),
            "source_presence": "ACTIVE",
            "stability_status": status,
            "stability_evidence": evidence,
            "product_url": str(product.get("product_url") or ""),
            "images_sha256": content_sha256(images),
            "image_resource_count": len(images),
            "good_details_sha256": content_sha256(product.get("shijiu_good_details") or product.get("description") or ""),
            "variants": variants,
            "observed_at": captured_at,
        }
        record["source_content_sha256"] = content_sha256({
            key: value for key, value in record.items() if key != "observed_at"
        })
        records[number] = record
    return records


def validate_stable_source_of_truth(
    stable_catalog: dict[str, Any], current_records: dict[str, dict[str, Any]]
) -> None:
    if stable_catalog.get("catalog_kind") != "MIKIHOUSE_STABLE_REGULAR_PRODUCT_POOL":
        raise SyncCycleError("stable_catalog is not the formal stable regular product pool")
    catalog_numbers = {
        str(row.get("product_number"))
        for row in stable_catalog.get("products") or []
        if row.get("active", True)
    }
    classified_numbers = {
        number for number, row in current_records.items() if row["stability_status"] == STABLE
    }
    if catalog_numbers != classified_numbers:
        missing = sorted(classified_numbers - catalog_numbers)[:10]
        leaked = sorted(catalog_numbers - classified_numbers)[:10]
        raise SyncCycleError(
            f"stable_catalog/source classification mismatch: missing={missing}, leaked={leaked}"
        )


def apply_stable_source_of_truth(
    stable_catalog: dict[str, Any],
    current_records: dict[str, dict[str, Any]],
    special: set[str],
    captured_at: str,
) -> dict[str, dict[str, Any]]:
    """Replace eligible records with the formal stable_catalog representation.

    The complete source snapshot remains necessary to classify exclusions and prove
    disappearance. Eligible data used for Shijiu diffs must, however, come from the
    stable catalog rather than a parallel normalization path.
    """
    stable_products = []
    for product in stable_catalog.get("products") or []:
        if not product.get("active", True):
            continue
        current_product = copy.deepcopy(product)
        current_product["variants"] = [
            row for row in current_product.get("variants") or [] if row.get("active", True)
        ]
        stable_products.append(current_product)
    formal_records = build_current_records(stable_products, special, captured_at)
    result = copy.deepcopy(current_records)
    for number, record in formal_records.items():
        if record["stability_status"] != STABLE:
            raise SyncCycleError(f"unstable product leaked into formal stable_catalog: {number}")
        source_record = result[number]
        if record["images_sha256"] != source_record["images_sha256"]:
            raise SyncCycleError(f"stable_catalog image set is stale versus complete snapshot: {number}")
        if set(record["variants"]) != set(source_record["variants"]):
            raise SyncCycleError(f"stable_catalog variant set is stale versus complete snapshot: {number}")
        for sku, formal_variant in record["variants"].items():
            source_variant = source_record["variants"][sku]
            comparable = (
                "tax_included_price_jpy",
                "compare_at_price_jpy",
                "available_for_sale",
                "color",
                "size",
                "selected_options",
                "variant_image_sha256",
            )
            if any(formal_variant.get(key) != source_variant.get(key) for key in comparable):
                raise SyncCycleError(
                    f"stable_catalog variant data is stale versus complete snapshot: {number}::{sku}"
                )
        result[number] = record
    return result


def empty_sync_state() -> dict[str, Any]:
    return {
        "schema_version": SYNC_STATE_SCHEMA_VERSION,
        "source": SOURCE,
        "target": TARGET,
        "identity": {
            "product": "product_number",
            "variant": "product_number::variant SKU",
        },
        "cycle_number": 0,
        "last_successful_crawl": None,
        "products": {},
        "event_ledger": {},
        "consumed_event_ids": [],
        "pending_action_event_ids": [],
        "permanent_exclusions": {},
    }


def load_sync_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_sync_state()
    state = _read_json(path)
    if state.get("schema_version") != SYNC_STATE_SCHEMA_VERSION:
        raise SyncCycleError("unsupported MIKIHOUSE source sync state schema")
    if state.get("source") != SOURCE or state.get("target") != TARGET:
        raise SyncCycleError("sync state source ownership mismatch")
    return state


def _bound_mapping(mapping: dict[str, Any], product_number: str) -> dict[str, Any] | None:
    row = (mapping.get("products") or {}).get(product_number)
    if not row or row.get("shijiu_product_id") in (None, ""):
        return None
    if row.get("source") != SOURCE:
        raise SyncCycleError(f"cross-source mapping ownership mismatch: {product_number}")
    return row


def _price_assessment(before: dict[str, Any], after: dict[str, Any], guard: dict[str, Any]) -> dict[str, Any]:
    old_tax = int(before["tax_included_price_jpy"])
    new_tax = int(after["tax_included_price_jpy"])
    target = calculate_mini_program_price_jpy(new_tax)
    reasons: list[str] = []
    if not int(guard["minimum_tax_included_price_jpy"]) <= new_tax <= int(
        guard["maximum_tax_included_price_jpy"]
    ):
        reasons.append("new_tax_included_price_outside_valid_range")
    absolute = abs(new_tax - old_tax)
    if absolute > int(guard["maximum_absolute_change_jpy"]):
        reasons.append("absolute_price_change_exceeds_threshold")
    relative = None if old_tax <= 0 else Decimal(absolute) / Decimal(old_tax)
    if relative is None:
        reasons.append("non_positive_baseline_price")
    elif relative > Decimal(str(guard["maximum_relative_change_ratio"])):
        reasons.append("relative_price_change_exceeds_threshold")
    return {
        "passed": not reasons,
        "review_required": bool(reasons),
        "reasons": reasons,
        "before_tax_included_price_jpy": old_tax,
        "after_tax_included_price_jpy": new_tax,
        "target_price_jpy": target,
        "absolute_change_jpy": absolute,
        "relative_change_ratio": float(relative) if relative is not None else None,
        "currency": "JPY",
        "currency_conversion_applied": False,
    }


def _event(
    event_type: str,
    product_number: str,
    detected_at: str,
    *,
    variant_sku: str | None = None,
    before: Any = None,
    after: Any = None,
    reason: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    identity = {
        "event_type": event_type,
        "product_number": product_number,
        "variant_sku": variant_sku,
        "before": before,
        "after": after,
        "reason": reason,
        "details": details or {},
    }
    return {
        "schema_version": SYNC_EVENT_SCHEMA_VERSION,
        "event_id": content_sha256(identity),
        "detected_at": detected_at,
        "source": SOURCE,
        **identity,
    }


def _normal_restore_price_assessments(
    previous: dict[str, Any], current: dict[str, Any], guard: dict[str, Any]
) -> list[dict[str, Any]]:
    previous_stable = previous.get("last_stable_variants") or previous.get("variants") or {}
    assessments = []
    for sku, after in current["variants"].items():
        before = previous_stable.get(sku)
        if before is None:
            continue
        result = _price_assessment(before, after, guard)
        result["variant_sku"] = sku
        assessments.append(result)
    return assessments


def _diff_product(
    previous: dict[str, Any] | None,
    current: dict[str, Any] | None,
    *,
    product_number: str,
    mapped: bool,
    guard: dict[str, Any],
    detected_at: str,
) -> list[dict[str, Any]]:
    if current and current["stability_status"] == PDF_SPECIAL:
        return []
    if previous and previous.get("stability_status") == PDF_SPECIAL:
        return []
    if previous is None and current is not None:
        if current["stability_status"] == STABLE:
            events = [_event(NEW_PRODUCT, product_number, detected_at, after={
                "name": current["name"], "variant_count": len(current["variants"])
            })]
            events.extend(
                _event(NEW_VARIANT, product_number, detected_at, variant_sku=sku, after=variant)
                for sku, variant in sorted(current["variants"].items())
            )
            return events
        if current["stability_status"] == REVIEW_REQUIRED:
            return [_event(
                REVIEW_EVENT, product_number, detected_at,
                reason=REVIEW_REQUIRED,
                details={"new_product_create_suppressed": True},
            )]
        return [_event(
            NO_CHANGE, product_number, detected_at,
            reason=f"NEW_{current['stability_status']}_CREATE_SUPPRESSED",
        )]
    if previous is not None and current is None:
        if previous.get("source_presence") == "INACTIVE":
            return [_event(NO_CHANGE, product_number, detected_at, reason="ALREADY_SOURCE_INACTIVE")]
        events = [_event(
            PRODUCT_INACTIVE, product_number, detected_at,
            before="ACTIVE", after="INACTIVE", reason="ABSENT_FROM_COMPLETE_STOREFRONT_CRAWL",
        )]
        events.extend(
            _event(
                VARIANT_INACTIVE, product_number, detected_at, variant_sku=sku,
                before="ACTIVE", after="INACTIVE", reason="PRODUCT_ABSENT_FROM_COMPLETE_CRAWL",
            )
            for sku, row in sorted((previous.get("variants") or {}).items())
            if row.get("source_presence", "ACTIVE") == "ACTIVE"
        )
        return events
    if previous is None or current is None:
        raise AssertionError("unreachable product transition")

    events: list[dict[str, Any]] = []
    previous_status = str(previous.get("stability_status") or REVIEW_REQUIRED)
    current_status = current["stability_status"]
    was_inactive = previous.get("source_presence") == "INACTIVE"
    if was_inactive:
        events.append(_event(
            PRODUCT_REACTIVATED, product_number, detected_at,
            before="INACTIVE", after="ACTIVE", reason="RETURNED_IN_COMPLETE_STOREFRONT_CRAWL",
        ))

    if previous_status == STABLE and current_status in UNSTABLE_STATUSES:
        if mapped:
            events.append(_event(
                STABILITY_QUARANTINE, product_number, detected_at,
                before=STABLE, after=current_status, reason=current_status,
                details={
                    "future_target_semantics": "WHOLE_PRODUCT_TEMPORARY_OFF_SHELF",
                    "price_update_suppressed": True,
                    "mapping_preserved": True,
                },
            ))
        elif current_status == REVIEW_REQUIRED:
            events.append(_event(
                REVIEW_EVENT, product_number, detected_at, reason=REVIEW_REQUIRED,
                details={"unmapped_create_suppressed": True},
            ))
        else:
            events.append(_event(
                NO_CHANGE, product_number, detected_at,
                reason=f"UNMAPPED_{current_status}_NO_TARGET_ACTION",
            ))
        return events

    if previous_status in UNSTABLE_STATUSES and current_status == STABLE:
        if mapped:
            assessments = _normal_restore_price_assessments(previous, current, guard)
            failed = [row for row in assessments if row["review_required"]]
            if failed:
                events.append(_event(
                    REVIEW_EVENT, product_number, detected_at,
                    before=previous_status, after=STABLE,
                    reason="RESTORED_NORMAL_PRICE_FAILED_GUARD",
                    details={"price_assessments": failed, "restore_suppressed": True},
                ))
            else:
                events.append(_event(
                    STABILITY_RESTORED, product_number, detected_at,
                    before=previous_status, after=STABLE, reason="NORMAL_STABILITY_RESTORED",
                    details={
                        "normal_price_assessments": assessments,
                        "future_target_semantics": "ALLOW_RESTORE_AFTER_STRONG_VALIDATION",
                    },
                ))
        else:
            events.append(_event(
                NEW_PRODUCT, product_number, detected_at,
                before=previous_status, after={"status": STABLE, "variant_count": len(current["variants"])},
                reason="FIRST_SHIJIU_ELIGIBLE_STABLE_OBSERVATION",
            ))
        return events

    if current_status in UNSTABLE_STATUSES:
        if current_status != previous_status and mapped:
            events.append(_event(
                STABILITY_QUARANTINE, product_number, detected_at,
                before=previous_status, after=current_status, reason=current_status,
                details={"mapping_preserved": True, "price_update_suppressed": True},
            ))
        else:
            events.append(_event(NO_CHANGE, product_number, detected_at, reason=f"REMAINS_{current_status}"))
        return events

    previous_variants = previous.get("variants") or {}
    current_variants = current["variants"]
    for sku in sorted(current_variants.keys() - previous_variants.keys()):
        events.append(_event(
            NEW_VARIANT, product_number, detected_at, variant_sku=sku,
            after=current_variants[sku],
        ))
    for sku in sorted(previous_variants.keys() - current_variants.keys()):
        if previous_variants[sku].get("source_presence", "ACTIVE") == "ACTIVE":
            events.append(_event(
                VARIANT_INACTIVE, product_number, detected_at, variant_sku=sku,
                before="ACTIVE", after="INACTIVE", reason="ABSENT_FROM_COMPLETE_PRODUCT_VARIANT_CRAWL",
            ))
    for sku in sorted(previous_variants.keys() & current_variants.keys()):
        before = previous_variants[sku]
        after = current_variants[sku]
        if before.get("source_presence") == "INACTIVE":
            events.append(_event(
                VARIANT_REACTIVATED, product_number, detected_at, variant_sku=sku,
                before="INACTIVE", after="ACTIVE",
            ))
        if int(before["tax_included_price_jpy"]) != int(after["tax_included_price_jpy"]):
            assessment = _price_assessment(before, after, guard)
            if assessment["review_required"]:
                events.append(_event(
                    REVIEW_EVENT, product_number, detected_at, variant_sku=sku,
                    before=before["tax_included_price_jpy"],
                    after=after["tax_included_price_jpy"],
                    reason="PRICE_GUARD_REJECTED",
                    details={"price_assessment": assessment},
                ))
            else:
                events.append(_event(
                    PRICE_CHANGED, product_number, detected_at, variant_sku=sku,
                    before={
                        "tax_included_price_jpy": before["tax_included_price_jpy"],
                        "target_price_jpy": calculate_mini_program_price_jpy(
                            int(before["tax_included_price_jpy"])
                        ),
                    },
                    after={
                        "tax_included_price_jpy": after["tax_included_price_jpy"],
                        "target_price_jpy": after["target_price_jpy"],
                    },
                    details={"price_assessment": assessment},
                ))
        if bool(before["available_for_sale"]) != bool(after["available_for_sale"]):
            events.append(_event(
                INVENTORY_CHANGED, product_number, detected_at, variant_sku=sku,
                before={
                    "available_for_sale": bool(before["available_for_sale"]),
                    "target_stock": 1 if before["available_for_sale"] else 0,
                },
                after={
                    "available_for_sale": bool(after["available_for_sale"]),
                    "target_stock": after["target_stock"],
                },
                reason="BOOLEAN_AVAILABILITY_ONLY_NO_EXACT_STOCK_INVENTED",
            ))
    if previous.get("images_sha256") != current.get("images_sha256"):
        events.append(_event(
            IMAGE_CHANGED, product_number, detected_at,
            before={"images_sha256": previous.get("images_sha256"), "count": previous.get("image_resource_count")},
            after={"images_sha256": current.get("images_sha256"), "count": current.get("image_resource_count")},
            reason="ORDERED_OFFICIAL_SOURCE_RESOURCE_SET_CHANGED",
        ))
    if not events:
        events.append(_event(NO_CHANGE, product_number, detected_at, reason="STABLE_SOURCE_UNCHANGED"))
    return events


def _action_for_event(event: dict[str, Any], mapping: dict[str, Any]) -> dict[str, Any] | None:
    event_type = event["event_type"]
    if event_type in {NO_CHANGE, REVIEW_EVENT}:
        return None
    number = event["product_number"]
    mapped = _bound_mapping(mapping, number)
    sku = event.get("variant_sku")
    if event_type == NEW_PRODUCT:
        if mapped:
            return None
        action_type = "CREATE_PRODUCT"
    elif event_type == NEW_VARIANT:
        if not mapped:
            return None
        action_type = "ADD_VARIANT"
    elif not mapped:
        return None
    else:
        action_type = {
            PRICE_CHANGED: "UPDATE_PRICE",
            INVENTORY_CHANGED: "UPDATE_INVENTORY",
            IMAGE_CHANGED: "UPDATE_IMAGES",
            PRODUCT_INACTIVE: "DEACTIVATE_PRODUCT_SOURCE_INACTIVE",
            VARIANT_INACTIVE: "DEACTIVATE_VARIANT_SOURCE_INACTIVE",
            PRODUCT_REACTIVATED: "REACTIVATE_PRODUCT_SOURCE_RETURNED",
            VARIANT_REACTIVATED: "REACTIVATE_VARIANT_SOURCE_RETURNED",
            STABILITY_QUARANTINE: "DEACTIVATE_PRODUCT_STABILITY_QUARANTINE",
            STABILITY_RESTORED: "REACTIVATE_PRODUCT_STABILITY_RESTORED",
        }.get(event_type)
    if not action_type:
        return None
    variant_mapping = ((mapped or {}).get("variants") or {}).get(sku) or {}
    target_values = copy.deepcopy(event.get("after"))
    if event_type == STABILITY_RESTORED:
        target_values = {
            "stability_status": STABLE,
            "normal_price_assessments": copy.deepcopy(
                (event.get("details") or {}).get("normal_price_assessments") or []
            ),
        }
    return {
        "action_id": content_sha256({"event_id": event["event_id"], "action_type": action_type}),
        "source_event_id": event["event_id"],
        "source": SOURCE,
        "target": TARGET,
        "source_product_id": f"MIKIHOUSE:{number}",
        "source_variant_id": f"MIKIHOUSE:{number}:{sku}" if sku else None,
        "target_category_id": TARGET_CATEGORY_ID,
        "action_type": action_type,
        "product_number": number,
        "variant_sku": sku,
        "shijiu_product_id": (mapped or {}).get("shijiu_product_id"),
        "backend_sku_code": variant_mapping.get("backend_sku_code") or (f"MIKI-{sku}" if sku else None),
        "target_values": target_values,
        "source_event_details": copy.deepcopy(event.get("details") or {}),
        "execution_mode": PLANNING_ONLY,
        "execution_allowed": False,
        "blocked_by": [
            "CURRENT_TASK_PROHIBITS_ALL_SHIJIU_WRITES",
            "PRODUCTION_WRITER_MUTEX_NOT_REQUESTED_WAWU_MAY_BE_ACTIVE",
        ],
        "write_executed": False,
    }


def _pending_action_still_matches_current_source(
    event: dict[str, Any],
    current_records: dict[str, dict[str, Any]],
    special: set[str],
    mapping: dict[str, Any],
) -> bool:
    number = str(event.get("product_number") or "")
    event_type = event.get("event_type")
    if number in special:
        return False
    current = current_records.get(number)
    current_status = (current or {}).get("stability_status")
    sku = event.get("variant_sku")
    current_variant = ((current or {}).get("variants") or {}).get(sku)
    mapped = _bound_mapping(mapping, number) is not None
    if event_type == NEW_PRODUCT:
        return current is not None and current_status == STABLE and not mapped
    if event_type == NEW_VARIANT:
        return current is not None and current_status == STABLE and current_variant is not None
    if event_type == PRICE_CHANGED:
        after = event.get("after") or {}
        return (
            current_status == STABLE
            and current_variant is not None
            and int(after.get("tax_included_price_jpy", -1))
            == int(current_variant["tax_included_price_jpy"])
            and int(after.get("target_price_jpy", -1)) == int(current_variant["target_price_jpy"])
        )
    if event_type == INVENTORY_CHANGED:
        after = event.get("after") or {}
        return (
            current_status == STABLE
            and current_variant is not None
            and int(after.get("target_stock", -1)) == int(current_variant["target_stock"])
        )
    if event_type == IMAGE_CHANGED:
        return (
            current_status == STABLE
            and (event.get("after") or {}).get("images_sha256") == current.get("images_sha256")
        )
    if event_type == PRODUCT_INACTIVE:
        return current is None and mapped
    if event_type == VARIANT_INACTIVE:
        return (current is None or current_variant is None) and mapped
    if event_type in {PRODUCT_REACTIVATED, VARIANT_REACTIVATED}:
        return current_status == STABLE and (event_type == PRODUCT_REACTIVATED or current_variant is not None) and mapped
    if event_type == STABILITY_QUARANTINE:
        return current is not None and current_status in UNSTABLE_STATUSES and mapped
    if event_type == STABILITY_RESTORED:
        return current_status == STABLE and mapped
    return False


def _merge_current_into_state(
    previous_products: dict[str, Any], current_records: dict[str, Any], captured_at: str
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for number in sorted(previous_products.keys() | current_records.keys()):
        previous = previous_products.get(number)
        current = current_records.get(number)
        if current is None:
            row = copy.deepcopy(previous)
            if row.get("source_presence") != "INACTIVE":
                row["source_presence"] = "INACTIVE"
                row["inactivated_at"] = captured_at
                for variant in (row.get("variants") or {}).values():
                    variant["source_presence"] = "INACTIVE"
            merged[number] = row
            continue
        row = copy.deepcopy(current)
        row["first_seen_at"] = (previous or {}).get("first_seen_at", captured_at)
        row["last_seen_at"] = captured_at
        row["inactivated_at"] = None
        if row["stability_status"] == STABLE:
            row["last_stable_at"] = captured_at
            row["last_stable_variants"] = copy.deepcopy(row["variants"])
        elif previous:
            row["last_stable_at"] = previous.get("last_stable_at")
            row["last_stable_variants"] = copy.deepcopy(previous.get("last_stable_variants") or {})
        merged[number] = row
    return merged


def plan_sync_cycle(
    previous_state: dict[str, Any],
    source_snapshot: dict[str, Any],
    stable_catalog: dict[str, Any],
    special: set[str],
    mapping: dict[str, Any],
    price_guard: dict[str, Any],
    *,
    initialize_baseline: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    products = validate_complete_snapshot(source_snapshot)
    if len(special) != 351:
        raise SyncCycleError(f"PDF special manifest must contain exactly 351 product numbers, got {len(special)}")
    captured_at = str(source_snapshot.get("captured_at") or utc_now())
    current_records = build_current_records(products, special, captured_at)
    validate_stable_source_of_truth(stable_catalog, current_records)
    current_records = apply_stable_source_of_truth(
        stable_catalog, current_records, special, captured_at
    )
    if previous_state.get("source") != SOURCE or previous_state.get("target") != TARGET:
        raise SyncCycleError("previous state has invalid source ownership")
    previous_products = previous_state.get("products") or {}
    is_initial = not previous_products
    if initialize_baseline and not is_initial:
        raise SyncCycleError("baseline initialization is only valid for an empty source state")

    generated: list[dict[str, Any]] = []
    if not (initialize_baseline and is_initial):
        for number in sorted(previous_products.keys() | current_records.keys()):
            if number in special:
                continue
            mapped = _bound_mapping(mapping, number) is not None
            generated.extend(_diff_product(
                previous_products.get(number),
                current_records.get(number),
                product_number=number,
                mapped=mapped,
                guard=price_guard,
                detected_at=captured_at,
            ))

    ledger = copy.deepcopy(previous_state.get("event_ledger") or {})
    consumed = set(previous_state.get("consumed_event_ids") or [])
    new_events: list[dict[str, Any]] = []
    newly_planned_actions: list[dict[str, Any]] = []
    for event in generated:
        event_id = event["event_id"]
        if event_id in ledger:
            continue
        ledger[event_id] = {
            "event_type": event["event_type"],
            "product_number": event["product_number"],
            "variant_sku": event.get("variant_sku"),
            "first_detected_at": event["detected_at"],
            "status": "OBSERVED_NO_ACTION" if event["event_type"] in {NO_CHANGE, REVIEW_EVENT} else "PLANNED",
            "event": copy.deepcopy(event),
        }
        new_events.append(event)
        if event_id not in consumed:
            action = _action_for_event(event, mapping)
            if action:
                newly_planned_actions.append(action)

    pending_ids = set(previous_state.get("pending_action_event_ids") or [])
    pending_ids.update(action["source_event_id"] for action in newly_planned_actions)
    pending_ids.difference_update(consumed)
    for event_id in list(pending_ids):
        stored_event = (ledger.get(event_id) or {}).get("event")
        if not stored_event:
            raise SyncCycleError(f"pending event lacks normalized event payload: {event_id}")
        if not _pending_action_still_matches_current_source(
            stored_event, current_records, special, mapping
        ):
            pending_ids.remove(event_id)
            ledger[event_id]["status"] = "SUPERSEDED_BY_CURRENT_SOURCE_STATE"
    actions: list[dict[str, Any]] = []
    for event_id in sorted(pending_ids):
        stored_event = (ledger.get(event_id) or {}).get("event")
        if not stored_event:
            raise SyncCycleError(f"pending event lacks normalized event payload: {event_id}")
        action = _action_for_event(stored_event, mapping)
        if action:
            actions.append(action)
    merged_products = _merge_current_into_state(previous_products, current_records, captured_at)
    new_state = copy.deepcopy(previous_state)
    new_state.update({
        "schema_version": SYNC_STATE_SCHEMA_VERSION,
        "source": SOURCE,
        "target": TARGET,
        "cycle_number": int(previous_state.get("cycle_number") or 0) + 1,
        "last_successful_crawl": {
            "captured_at": captured_at,
            "complete_pagination_validated": True,
            "storefront_product_count": len(products),
            "source_snapshot_sha256": content_sha256(source_snapshot),
            "stable_catalog_active_product_count": sum(
                row.get("active", True) for row in stable_catalog.get("products") or []
            ),
        },
        "products": merged_products,
        "event_ledger": ledger,
        "consumed_event_ids": sorted(consumed),
        "pending_action_event_ids": sorted(pending_ids),
        "permanent_exclusions": {
            PDF_SPECIAL: {
                "count": len(special),
                "product_numbers": sorted(special),
                "policy": "never emit any Shijiu create/update/stock/image/price/reactivation action",
            },
            NON_SELLABLE_SERVICE_OR_ADDON: {
                "count": len(PERMANENT_NON_SELLABLE_PRODUCT_NUMBERS),
                "product_numbers": sorted(PERMANENT_NON_SELLABLE_PRODUCT_NUMBERS),
                "policy": "never emit Shijiu create/price/inventory/image/reactivation actions",
            }
        },
    })
    event_counts = Counter(row["event_type"] for row in new_events)
    action_counts = Counter(row["action_type"] for row in actions)
    report = {
        "schema_version": SYNC_STATE_SCHEMA_VERSION,
        "status": "BASELINE_INITIALIZED_PLANNING_ONLY" if initialize_baseline and is_initial else "SYNC_CYCLE_PLANNED_NO_WRITE",
        "mode": PLANNING_ONLY,
        "source": SOURCE,
        "target": TARGET,
        "captured_at": captured_at,
        "cycle_number": new_state["cycle_number"],
        "baseline_initialization": bool(initialize_baseline and is_initial),
        "complete_crawl_required_and_validated": True,
        "stable_catalog_is_only_eligible_source_of_truth": True,
        "counts": {
            "source_product_count": len(products),
            "source_variant_count": sum(len(row["variants"]) for row in current_records.values()),
            "stable_product_count": sum(row["stability_status"] == STABLE for row in current_records.values()),
            "pdf_special_count_online": sum(row["stability_status"] == PDF_SPECIAL for row in current_records.values()),
            "pdf_special_count_offline_remembered": len(
                special - {
                    number for number, row in current_records.items()
                    if row["stability_status"] == PDF_SPECIAL
                }
            ),
            "web_exclusive_count": sum(row["stability_status"] == WEB_EXCLUSIVE for row in current_records.values()),
            "limited_time_price_count": sum(row["stability_status"] == LIMITED_TIME_PRICE for row in current_records.values()),
            "non_sellable_service_or_addon_count": sum(
                row["stability_status"] == NON_SELLABLE_SERVICE_OR_ADDON
                for row in current_records.values()
            ),
            "review_required_stability_count": sum(row["stability_status"] == REVIEW_REQUIRED for row in current_records.values()),
            "new_event_count": len(new_events),
            "new_action_count": len(newly_planned_actions),
            "event_counts": dict(sorted(event_counts.items())),
            "pending_action_counts": dict(sorted(action_counts.items())),
            "pending_action_event_count": len(pending_ids),
        },
        "safety": {
            "shijiu_requests": 0,
            "shijiu_create_requests": 0,
            "shijiu_update_requests": 0,
            "shijiu_cos_upload_requests": 0,
            "shijiu_shelf_price_inventory_writes": 0,
            "legacy_286_touched": False,
            "writer_mutex_evidence_generated": False,
            "operator_reported_wawu_may_be_active": True,
            "planning_only_hard_stop": True,
        },
        "event_file_contains_only_new_unique_events": True,
        "action_file_contains_all_unconsumed_pending_actions": True,
        "identical_snapshot_replay": (
            (previous_state.get("last_successful_crawl") or {}).get("source_snapshot_sha256")
            == content_sha256(source_snapshot)
        ),
        "idempotent_replay_produced_no_new_events": (
            bool(previous_products)
            and (previous_state.get("last_successful_crawl") or {}).get("source_snapshot_sha256")
            == content_sha256(source_snapshot)
            and not new_events
        ),
        "actions_are_non_executable": all(not row["execution_allowed"] for row in actions),
    }
    return new_state, report, new_events, actions


def mark_event_consumed(state: dict[str, Any], event_id: str) -> dict[str, Any]:
    if event_id not in (state.get("event_ledger") or {}):
        raise SyncCycleError("cannot consume an unknown event")
    result = copy.deepcopy(state)
    consumed = set(result.get("consumed_event_ids") or [])
    consumed.add(event_id)
    result["consumed_event_ids"] = sorted(consumed)
    result["pending_action_event_ids"] = sorted(
        set(result.get("pending_action_event_ids") or []) - {event_id}
    )
    result["event_ledger"][event_id]["status"] = "CONSUMED_AFTER_VERIFIED_TARGET_READBACK"
    return result


def build_readiness_report(cycle_report: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "status": "READY_FOR_REPEATABLE_OFFLINE_PLANNING_TARGET_WRITES_BLOCKED",
        "production_write_status": "PROHIBITED_WAWU_MAY_BE_ACTIVE",
        "source_of_truth": "output/storefront-stable/stable_catalog.json",
        "last_successful_source_state": state.get("last_successful_crawl"),
        "recognized_event_types": list(ALL_EVENT_TYPES),
        "future_shijiu_action_semantics": {
            NEW_PRODUCT: "CREATE only when STABLE, unmapped, and a future authorized mutex window exists",
            NEW_VARIANT: "add exact product_number::variant SKU; never fuzzy match",
            PRICE_CHANGED: "update exact backend SKU to ceil(normal tax-included JPY * 0.65)",
            INVENTORY_CHANGED: "available=true -> stock=1; false -> stock=0",
            IMAGE_CHANGED: "upload changed official source resources before full-payload update",
            PRODUCT_INACTIVE: "off-shelf only after a complete crawl proves disappearance",
            VARIANT_INACTIVE: "disable exact variant only after complete variant crawl",
            PRODUCT_REACTIVATED: "restore source-presence state after strong validation",
            VARIANT_REACTIVATED: "restore exact variant after strong validation",
            STABILITY_QUARANTINE: "whole-product temporary off-shelf; preserve mapping; suppress price",
            STABILITY_RESTORED: "use restored normal JPY price, guard it, then permit re-shelf",
            REVIEW_EVENT: "human review; no automatic target mutation",
            NO_CHANGE: "no target mutation",
        },
        "hard_guards": {
            "special_351_never_emit_shijiu_action": True,
            "non_sellable_services_never_emit_create_price_inventory_image_or_reactivation_action": True,
            "new_unstable_products_never_create": True,
            "promo_price_never_emits_price_update": True,
            "incomplete_crawl_never_advances_state_or_inactivates": True,
            "exact_inventory_not_invented": True,
            "currency": "JPY",
            "currency_conversion_applied": False,
            "writer_mutex_required_for_future_execution": True,
            "writer_mutex_requested_this_cycle": False,
        },
        "current_cycle": cycle_report,
        "implementation_boundary": {
            "manual_and_scheduled_entrypoint": "scripts/sync_mikihouse_cycle.py",
            "shared_core": "mikihouse_luyao.stable_sync.plan_sync_cycle",
            "current_terminal_stage": PLANNING_ONLY,
            "shijiu_client_imported_or_called": False,
        },
    }


def run_cycle(
    *,
    source_path: Path,
    stable_path: Path,
    special_path: Path,
    mapping_path: Path,
    price_guard_path: Path,
    state_path: Path,
    output_dir: Path,
    report_dir: Path,
    initialize_baseline: bool,
    trigger: str = "manual",
) -> dict[str, Any]:
    special = set(read_product_numbers(special_path))
    source_snapshot = _read_json(source_path)
    stable_catalog = _read_json(stable_path)
    mapping = _read_json(mapping_path)
    guard = load_price_guard(price_guard_path)
    previous = load_sync_state(state_path)
    new_state, report, events, actions = plan_sync_cycle(
        previous, source_snapshot, stable_catalog, special, mapping, guard,
        initialize_baseline=initialize_baseline,
    )
    report["trigger"] = trigger
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(output_dir / "normalized_events.json", {
        "schema_version": SYNC_EVENT_SCHEMA_VERSION,
        "mode": PLANNING_ONLY,
        "events": events,
    })
    write_json_atomic(output_dir / "shijiu_action_plan.json", {
        "schema_version": 1,
        "mode": PLANNING_ONLY,
        "execution_allowed": False,
        "actions": actions,
    })
    write_json_atomic(output_dir / "sync_cycle_report.json", report)
    if state_path.exists():
        backup = state_path.with_name(state_path.name.replace(".json.gz", ".previous.json.gz"))
        temporary_backup = backup.with_name(f".{backup.name}.tmp")
        temporary_backup.write_bytes(state_path.read_bytes())
        temporary_backup.replace(backup)
    write_json_atomic(state_path, new_state)
    report["outputs"] = {
        "source_state": str(state_path),
        "source_state_file_sha256": file_sha256(state_path),
        "source_state_logical_sha256": content_sha256(new_state),
        "normalized_events": str(output_dir / "normalized_events.json"),
        "shijiu_action_plan": str(output_dir / "shijiu_action_plan.json"),
        "local_sync_cycle_report": str(output_dir / "sync_cycle_report.json"),
    }
    report["protected_state_update"] = {
        "reason": "persist last complete MIKIHOUSE source snapshot and idempotent event ledger",
        "atomic_replace": True,
        "previous_snapshot_kept_outside_git": True,
    }
    report_dir.mkdir(parents=True, exist_ok=True)
    readiness = build_readiness_report(report, new_state)
    write_json_atomic(output_dir / "sync_cycle_report.json", report)
    write_json_atomic(report_dir / "sync_cycle_planning_report.json", report)
    write_json_atomic(report_dir / "future_automatic_sync_readiness.json", readiness)
    return readiness


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the shared manual/scheduled MIKIHOUSE sync cycle in planning-only mode"
    )
    parser.add_argument("--trigger", choices=("manual", "scheduled"), default="manual")
    parser.add_argument("--refresh-storefront", action="store_true")
    parser.add_argument("--initialize-baseline", action="store_true")
    parser.add_argument("--special", type=Path, default=Path("special_skus_2026aw.csv"))
    parser.add_argument("--source", type=Path, default=Path("output/storefront-stable/source_catalog.json"))
    parser.add_argument("--stable", type=Path, default=Path("output/storefront-stable/stable_catalog.json"))
    parser.add_argument("--mapping", type=Path, default=Path("state/shijiu_mappings.json"))
    parser.add_argument("--price-guard", type=Path, default=Path("config/shijiu_price_guard.json"))
    parser.add_argument("--state", type=Path, default=Path("state/mikihouse_source_sync_state.json.gz"))
    parser.add_argument("--output", type=Path, default=Path("output/storefront-sync-cycle"))
    parser.add_argument("--report-dir", type=Path, default=Path("deliverables/storefront_stable_catalog"))
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--delay", type=float, default=0.1)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--retries", type=int, default=2)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.refresh_storefront:
        run_stable_catalog_sync(
            args.special,
            args.source.parent,
            args.report_dir,
            old_master_path=Path("output/storefront-master/master_catalog.json"),
            page_size=args.page_size,
            delay=args.delay,
            timeout=args.timeout,
            retries=args.retries,
            max_pages=1000,
        )
    readiness = run_cycle(
        source_path=args.source,
        stable_path=args.stable,
        special_path=args.special,
        mapping_path=args.mapping,
        price_guard_path=args.price_guard,
        state_path=args.state,
        output_dir=args.output,
        report_dir=args.report_dir,
        initialize_baseline=args.initialize_baseline,
        trigger=args.trigger,
    )
    print(json.dumps(readiness, ensure_ascii=False, indent=2))
    return 0
