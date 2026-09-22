from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .catalog import _classification
from .csv_input import read_product_numbers
from .daily_quote_fx import FrozenFxRate
from .stable_catalog import (
    EXCLUDED,
    LIMITED_TIME_PRICE,
    NON_SELLABLE_SERVICE_OR_ADDON,
    PDF_SPECIAL,
    REVIEW_REQUIRED,
    WEB_EXCLUSIVE,
    assess_product_stability,
)


DAILY_QUOTE_SCHEMA_VERSION = 1
DAILY_QUOTE_ELIGIBLE = "DAILY_QUOTE_ELIGIBLE"
NO_DISCOUNT_LIST = "NO_DISCOUNT_LIST"
NON_WHITELIST_CATEGORY = "NON_WHITELIST_CATEGORY"
UNSAFE_QUOTE_DATA = "UNSAFE_QUOTE_DATA"
ALL_VARIANTS_UNAVAILABLE = "ALL_VARIANTS_UNAVAILABLE"
ALLOWED_CATEGORIES = ("footwear", "baby", "apparel")
CATEGORY_LABELS = {"footwear": "鞋类", "baby": "婴幼儿", "apparel": "服装"}
_PRODUCT_NUMBER_RE = re.compile(r"^\d{2}-\d{4}-\d{3}$")


class DailyQuoteError(RuntimeError):
    pass


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def calculate_customer_price_cny(
    tax_included_price_jpy: int, discount_rate: Decimal, jpy_to_cny_rate: Decimal
) -> int:
    if tax_included_price_jpy <= 0:
        raise ValueError("tax-included JPY price must be positive")
    if discount_rate <= 0 or jpy_to_cny_rate <= 0:
        raise ValueError("discount and FX rates must be positive")
    value = Decimal(tax_included_price_jpy) * discount_rate * jpy_to_cny_rate
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _source_product_for_hash(product: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(product)
    for key in ("first_seen_at", "last_seen_at", "inactivated_at"):
        result.pop(key, None)
    for variant in result.get("variants") or []:
        for key in ("first_seen_at", "last_seen_at", "inactivated_at", "mini_program_price_jpy"):
            variant.pop(key, None)
    return result


def source_snapshot_hash(products: Iterable[dict[str, Any]]) -> str:
    normalized = sorted((_source_product_for_hash(row) for row in products), key=lambda row: row["product_number"])
    return sha256_json(normalized)


def _quality_issue(product: dict[str, Any]) -> str | None:
    number = str(product.get("product_number") or "")
    if not _PRODUCT_NUMBER_RE.fullmatch(number):
        return "INVALID_PRODUCT_NUMBER"
    main_url = str((product.get("main_image") or {}).get("url") or "")
    if not main_url.startswith("https://"):
        return "MISSING_OR_NON_HTTPS_MAIN_IMAGE"
    variants = product.get("variants") or []
    if not variants:
        return "MISSING_VARIANTS"
    seen: set[str] = set()
    for variant in variants:
        sku = str(variant.get("sku") or "").strip()
        if not sku:
            return "MISSING_VARIANT_SKU"
        if sku in seen:
            return "DUPLICATE_VARIANT_SKU"
        seen.add(sku)
        try:
            if int(variant.get("tax_included_price_jpy")) <= 0:
                return "NON_POSITIVE_PRICE"
        except (TypeError, ValueError):
            return "INVALID_PRICE"
    return None


def assess_daily_quote_product(
    product: dict[str, Any], special_numbers: set[str]
) -> tuple[str, str | None, dict[str, Any]]:
    number = str(product.get("product_number") or "").strip()
    if number in special_numbers:
        return "EXCLUDED", NO_DISCOUNT_LIST, {"source_reason": PDF_SPECIAL}
    stability = assess_product_stability(product, special_numbers)
    if stability.get("status") == EXCLUDED:
        reason = str(stability.get("excluded_reason") or "")
        if reason == PDF_SPECIAL:
            reason = NO_DISCOUNT_LIST
        return "EXCLUDED", reason, {"stability": stability}
    if stability.get("status") == REVIEW_REQUIRED:
        return "EXCLUDED", REVIEW_REQUIRED, {"stability": stability}
    category = _classification(product) or "other"
    if category not in ALLOWED_CATEGORIES:
        return "EXCLUDED", NON_WHITELIST_CATEGORY, {"category": category}
    quality = _quality_issue(product)
    if quality:
        return "EXCLUDED", UNSAFE_QUOTE_DATA, {"quality_issue": quality}
    available = [row for row in product.get("variants") or [] if row.get("available_for_sale") is True]
    if not available:
        return "EXCLUDED", ALL_VARIANTS_UNAVAILABLE, {"available_variant_count": 0}
    return DAILY_QUOTE_ELIGIBLE, None, {"category": category, "available_variant_count": len(available)}


def group_available_variants(variants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[int, dict[str, list[str]]] = {}
    for variant in variants:
        if variant.get("available_for_sale") is not True:
            continue
        price = int(variant["customer_price_cny"])
        color = str(variant.get("color") or "-").strip() or "-"
        size = str(variant.get("size") or "-").strip() or "-"
        colors = grouped.setdefault(price, {})
        sizes = colors.setdefault(color, [])
        if size not in sizes:
            sizes.append(size)
    return [
        {
            "customer_price_cny": price,
            "colors": [
                {"color": color, "sizes": sizes}
                for color, sizes in colors.items()
            ],
        }
        for price, colors in sorted(grouped.items())
    ]


def build_daily_quote_manifest(
    source_products: list[dict[str, Any]],
    *,
    special_numbers: set[str],
    fx: FrozenFxRate,
    discount_rate: Decimal = Decimal("0.68"),
    quote_date: str | None = None,
    generated_at: str | None = None,
    main_image_hashes: dict[str, str] | None = None,
    main_image_assets: dict[str, dict[str, Any]] | None = None,
    media_failures: dict[str, str] | None = None,
    crawl_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if len(special_numbers) != 351:
        raise DailyQuoteError(f"no-discount manifest must contain exactly 351 products, got {len(special_numbers)}")
    now = datetime.now(ZoneInfo("Asia/Tokyo"))
    quote_date = quote_date or now.date().isoformat()
    generated_at = generated_at or now.isoformat()
    image_hashes = main_image_hashes or {}
    image_assets = main_image_assets or {}
    media_failures = media_failures or {}
    excluded_counts: Counter[str] = Counter()
    excluded_products: list[dict[str, Any]] = []
    included: list[dict[str, Any]] = []
    whitelist_counts: Counter[str] = Counter()
    for product in sorted(source_products, key=lambda row: row["product_number"]):
        category = _classification(product) or "other"
        if category in ALLOWED_CATEGORIES:
            whitelist_counts[category] += 1
        if product.get("product_number") in media_failures:
            excluded_counts[UNSAFE_QUOTE_DATA] += 1
            excluded_products.append({
                "product_number": product.get("product_number"),
                "name": product.get("name"),
                "category": category,
                "excluded_reason": UNSAFE_QUOTE_DATA,
                "evidence": {"quality_issue": "MAIN_IMAGE_DOWNLOAD_OR_DECODE_FAILED", "error": media_failures[product["product_number"]]},
            })
            continue
        status, reason, evidence = assess_daily_quote_product(product, special_numbers)
        if status != DAILY_QUOTE_ELIGIBLE:
            assert reason
            excluded_counts[reason] += 1
            excluded_products.append({
                "product_number": product.get("product_number"),
                "name": product.get("name"),
                "category": category,
                "excluded_reason": reason,
                "evidence": evidence,
            })
            continue
        available_variants: list[dict[str, Any]] = []
        all_variant_states: list[dict[str, Any]] = []
        for variant in product.get("variants") or []:
            all_variant_states.append({
                "stable_id": f"{product['product_number']}::{variant['sku']}",
                "sku": str(variant["sku"]),
                "available_for_sale": bool(variant.get("available_for_sale")),
                "tax_included_price_jpy": int(variant["tax_included_price_jpy"]),
            })
            if variant.get("available_for_sale") is not True:
                continue
            source_price = int(variant["tax_included_price_jpy"])
            available_variants.append({
                "stable_id": f"{product['product_number']}::{variant['sku']}",
                "sku": str(variant["sku"]),
                "color": str(variant.get("color") or ""),
                "size": str(variant.get("size") or ""),
                "available_for_sale": True,
                "tax_included_price_jpy": source_price,
                "customer_price_cny": calculate_customer_price_cny(
                    source_price, discount_rate, fx.jpy_to_cny_rate
                ),
            })
        main_image = copy.deepcopy(product["main_image"])
        main_image["content_sha256"] = image_hashes.get(product["product_number"], "")
        if product["product_number"] in image_assets:
            asset = image_assets[product["product_number"]]
            main_image["thumbnail"] = {
                "cache_path": asset["thumbnail_path"],
                "sha256": asset["thumbnail_sha256"],
                "byte_count": asset["thumbnail_byte_count"],
                "width": asset["thumbnail_width"],
                "height": asset["thumbnail_height"],
            }
        content_core = {
            "product_number": product["product_number"],
            "name": product["name"],
            "category": category,
            "variants": available_variants,
            "main_image": main_image,
        }
        included.append({
            **content_core,
            "product_url": product.get("product_url") or "",
            "variant_price_groups": group_available_variants(available_variants),
            "all_variant_states": all_variant_states,
            "product_content_sha256": sha256_json(content_core),
        })
    counts = {
        "storefront_product_count": len(source_products),
        "whitelist_category_product_count": sum(whitelist_counts.values()),
        "whitelist_category_counts": {key: whitelist_counts[key] for key in ALLOWED_CATEGORIES},
        "eligible_product_count": len(included),
        "included_in_stock_product_count": len(included),
        "included_available_variant_count": sum(len(row["variants"]) for row in included),
        "excluded_reason_counts": dict(sorted(excluded_counts.items())),
    }
    manifest = {
        "schema_version": DAILY_QUOTE_SCHEMA_VERSION,
        "manifest_kind": "MIKIHOUSE_DAILY_QUOTE",
        "business_boundary": DAILY_QUOTE_ELIGIBLE,
        "quote_date": quote_date,
        "generated_at": generated_at,
        "timezone": "Asia/Tokyo",
        "source_snapshot_sha256": source_snapshot_hash(source_products),
        "source_snapshot_product_count": len(source_products),
        "crawl": copy.deepcopy(crawl_stats or {}),
        "fx": fx.to_dict(),
        "discount_rate": format(discount_rate, "f"),
        "counts": counts,
        "products": included,
        "excluded_products": excluded_products,
        "customer_output_policy": {
            "only_available_variants": True,
            "forbidden_fields": ["source JPY price", "discount rate", "FX rate", "pricing formula"],
        },
    }
    manifest["manifest_sha256"] = sha256_json({k: v for k, v in manifest.items() if k != "manifest_sha256"})
    return manifest


def load_special_numbers(path: Path) -> set[str]:
    values = set(read_product_numbers(path))
    if len(values) != 351:
        raise DailyQuoteError(f"expected 351 no-discount product numbers, got {len(values)}")
    return values


def compare_manifests(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    before = {row["product_number"]: row for row in (previous or {}).get("products") or []}
    after = {row["product_number"]: row for row in current.get("products") or []}
    new_products = sorted(set(after) - set(before))
    exited = sorted(set(before) - set(after))
    jpy_changes: list[dict[str, Any]] = []
    cny_changes: list[dict[str, Any]] = []
    sold_out: list[str] = []
    restored: list[str] = []
    new_variants: list[str] = []
    for number in sorted(set(before) & set(after)):
        old_variants = {row["sku"]: row for row in before[number].get("all_variant_states") or before[number].get("variants") or []}
        new_variant_map = {row["sku"]: row for row in after[number].get("all_variant_states") or after[number].get("variants") or []}
        for sku in sorted(set(new_variant_map) - set(old_variants)):
            new_variants.append(f"{number}::{sku}")
        for sku in sorted(set(old_variants) & set(new_variant_map)):
            old, new = old_variants[sku], new_variant_map[sku]
            if old.get("available_for_sale") is True and new.get("available_for_sale") is False:
                sold_out.append(f"{number}::{sku}")
            elif old.get("available_for_sale") is False and new.get("available_for_sale") is True:
                restored.append(f"{number}::{sku}")
            if old["tax_included_price_jpy"] != new["tax_included_price_jpy"]:
                jpy_changes.append({"stable_id": f"{number}::{sku}", "before": old["tax_included_price_jpy"], "after": new["tax_included_price_jpy"]})
            old_customer = next((row["customer_price_cny"] for row in before[number].get("variants") or [] if row["sku"] == sku), None)
            new_customer = next((row["customer_price_cny"] for row in after[number].get("variants") or [] if row["sku"] == sku), None)
            if old_customer is not None and new_customer is not None and old_customer != new_customer:
                cny_changes.append({"stable_id": f"{number}::{sku}", "before": old_customer, "after": new_customer})
    return {
        "baseline_status": "NO_PREVIOUS_SUCCESSFUL_MANIFEST" if previous is None else "COMPARED",
        "new_products": new_products,
        "exited_products": exited,
        "source_jpy_price_changes": jpy_changes,
        "customer_cny_price_changes": cny_changes,
        "newly_sold_out_variants": sold_out,
        "restored_available_variants": restored,
        "new_variants": new_variants,
    }
