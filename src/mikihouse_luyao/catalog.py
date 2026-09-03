from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import io
import json
import os
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Any
from urllib.request import Request

from .csv_input import read_product_numbers
from .scraper import (
    BASE_URL,
    STOREFRONT_API_URL,
    STOREFRONT_TOKEN,
    USER_AGENT,
    ScrapeError,
    _request_with_retries,
    is_footwear_product,
)


CATALOG_SCHEMA_VERSION = 1
MINI_PROGRAM_DISCOUNT_RATE = Decimal("0.65")
CATALOG_QUERY = """
query CatalogPage($first: Int!, $after: String) {
  products(first: $first, after: $after, sortKey: ID) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      handle
      title
      vendor
      productType
      tags
      category { id name }
      onlineStoreUrl
      featuredImage { url width height altText }
      variants(first: 100) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          title
          sku
          availableForSale
          selectedOptions { name value }
          image { url width height altText }
          price { amount currencyCode }
        }
      }
    }
  }
}
"""
VARIANT_PAGE_QUERY = """
query CatalogVariantPage($handle: String!, $after: String) {
  product(handle: $handle) {
    handle
    variants(first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        title
        sku
        availableForSale
        selectedOptions { name value }
        image { url width height altText }
        price { amount currencyCode }
      }
    }
  }
}
"""


def calculate_mini_program_price_jpy(tax_included_price_jpy: int) -> int:
    """Return ceil(tax-included JPY price * 0.65), without currency conversion."""
    if tax_included_price_jpy < 0:
        raise ValueError("price must not be negative")
    value = Decimal(tax_included_price_jpy) * MINI_PROGRAM_DISCOUNT_RATE
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _parse_jpy_price(raw: dict[str, Any]) -> int:
    if raw.get("currencyCode") != "JPY":
        raise ScrapeError(f"unexpected currency: {raw.get('currencyCode')}")
    try:
        amount = Decimal(str(raw["amount"]))
    except Exception as exc:
        raise ScrapeError(f"invalid JPY price: {raw.get('amount')}") from exc
    if amount < 0 or amount != amount.to_integral_value():
        raise ScrapeError(f"JPY price must be a non-negative integer: {amount}")
    return int(amount)


def _image(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not raw or not raw.get("url"):
        return None
    return {
        "url": str(raw["url"]),
        "width": int(raw["width"]) if raw.get("width") else None,
        "height": int(raw["height"]) if raw.get("height") else None,
        "alt_text": str(raw.get("altText") or ""),
    }


def _options(raw: list[dict[str, Any]] | None, title: str) -> tuple[list[dict[str, str]], str, str]:
    selected = [
        {"name": str(item.get("name") or "").strip(), "value": str(item.get("value") or "").strip()}
        for item in (raw or [])
        if str(item.get("name") or "").strip()
    ]
    by_name = {item["name"].casefold(): item["value"] for item in selected}
    color = by_name.get("カラー") or by_name.get("color") or by_name.get("colour") or ""
    size = by_name.get("サイズ") or by_name.get("size") or ""
    parts = [part.strip() for part in title.split("/")]
    if not color and len(parts) >= 2:
        color = parts[0]
    if not size:
        size = parts[1] if len(parts) >= 2 else title.strip()
    return selected, color, size


def _normalize_variant(
    product_number: str,
    raw: dict[str, Any],
    featured_image: dict[str, Any] | None,
    synced_at: str,
) -> dict[str, Any]:
    sku = str(raw.get("sku") or "").strip()
    if not sku:
        raise ScrapeError(f"variant SKU is required for stable identity: {product_number}/{raw.get('id')}")
    title = str(raw.get("title") or "").strip()
    selected_options, color, size = _options(raw.get("selectedOptions"), title)
    price = _parse_jpy_price(raw.get("price") or {})
    variant_image = _image(raw.get("image"))
    resolved_image = variant_image or featured_image
    return {
        "stable_id": f"{product_number}::{sku}",
        "shopify_variant_id": str(raw.get("id") or ""),
        "sku": sku,
        "title": title,
        "active": True,
        "available_for_sale": bool(raw.get("availableForSale")),
        "selected_options": selected_options,
        "color": color,
        "size": size,
        "tax_included_price_jpy": price,
        "mini_program_price_jpy": calculate_mini_program_price_jpy(price),
        "variant_image": variant_image,
        "resolved_image": resolved_image,
        "image_source": "variant" if variant_image else ("featured_image_fallback" if featured_image else "none"),
        "first_seen_at": synced_at,
        "last_seen_at": synced_at,
        "inactivated_at": None,
    }


def normalize_product(raw: dict[str, Any], synced_at: str) -> dict[str, Any]:
    product_number = str(raw.get("handle") or "").strip()
    if not product_number:
        raise ScrapeError("product handle is required")
    featured_image = _image(raw.get("featuredImage"))
    raw_variants = list((raw.get("variants") or {}).get("nodes") or [])
    if not raw_variants:
        raise ScrapeError(f"product has no variants: {product_number}")
    variants = [_normalize_variant(product_number, item, featured_image, synced_at) for item in raw_variants]
    stable_ids = [item["stable_id"] for item in variants]
    if len(stable_ids) != len(set(stable_ids)):
        raise ScrapeError(f"duplicate variant SKU within product: {product_number}")

    color_images: list[dict[str, Any]] = []
    grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for variant in variants:
        image = variant["resolved_image"]
        if image:
            entry = grouped[variant["color"]].setdefault(
                image["url"],
                {"image": image, "image_source": variant["image_source"], "variant_skus": []},
            )
            entry["variant_skus"].append(variant["sku"])
    for color in sorted(grouped):
        color_images.append({
            "color": color,
            "images": [
                {**entry, "variant_skus": sorted(entry["variant_skus"])}
                for _, entry in sorted(grouped[color].items())
            ],
        })

    category = raw.get("category")
    tags = sorted({str(tag).strip() for tag in (raw.get("tags") or []) if str(tag).strip()})
    product_url = str(raw.get("onlineStoreUrl") or BASE_URL.format(product_number=product_number))
    return {
        "stable_id": product_number,
        "shopify_product_id": str(raw.get("id") or ""),
        "product_number": product_number,
        "handle": product_number,
        "name": str(raw.get("title") or "").strip(),
        "brand": str(raw.get("vendor") or "").strip(),
        "product_type": str(raw.get("productType") or "").strip(),
        "category": (
            {"id": str(category.get("id") or ""), "name": str(category.get("name") or "").strip()}
            if category
            else None
        ),
        "tags": tags,
        "main_image": featured_image,
        "color_images": color_images,
        "product_url": product_url,
        "active": True,
        "first_seen_at": synced_at,
        "last_seen_at": synced_at,
        "inactivated_at": None,
        "variants": sorted(variants, key=lambda item: item["stable_id"]),
    }


def _graphql_request(
    query: str,
    variables: dict[str, Any],
    timeout: float,
    retries: int,
) -> dict[str, Any]:
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = Request(
        STOREFRONT_API_URL,
        data=body,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "X-Shopify-Storefront-Access-Token": os.environ.get(
                "MIKIHOUSE_STOREFRONT_TOKEN", STOREFRONT_TOKEN
            ),
        },
    )
    raw = _request_with_retries(request, timeout, retries)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScrapeError("Storefront API returned invalid JSON") from exc
    if payload.get("errors"):
        messages = "; ".join(str(item.get("message", item)) for item in payload["errors"])
        raise ScrapeError(f"Storefront API GraphQL error: {messages}")
    return payload


def fetch_all_storefront_products(
    excluded_product_numbers: set[str],
    *,
    page_size: int = 100,
    delay: float = 0.1,
    timeout: float = 30,
    retries: int = 2,
    max_pages: int = 1000,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not 1 <= page_size <= 250:
        raise ValueError("page_size must be between 1 and 250")
    if delay < 0:
        raise ValueError("delay must not be negative")
    products: list[dict[str, Any]] = []
    excluded_seen: set[str] = set()
    seen_handles: set[str] = set()
    after: str | None = None
    product_page_count = 0
    extra_variant_page_count = 0
    api_request_count = 0
    synced_at = datetime.now(timezone.utc).isoformat()

    while True:
        if product_page_count >= max_pages:
            raise ScrapeError(f"catalog pagination exceeded {max_pages} pages")
        payload = _graphql_request(
            CATALOG_QUERY,
            {"first": page_size, "after": after},
            timeout,
            retries,
        )
        api_request_count += 1
        connection = ((payload.get("data") or {}).get("products") or {})
        raw_products = list(connection.get("nodes") or [])
        page_info = connection.get("pageInfo") or {}
        product_page_count += 1
        for raw_product in raw_products:
            handle = str(raw_product.get("handle") or "").strip()
            if not handle:
                raise ScrapeError("Storefront API returned a product without a handle")
            if handle in seen_handles:
                raise ScrapeError(f"duplicate product in catalog pagination: {handle}")
            seen_handles.add(handle)
            if handle in excluded_product_numbers:
                excluded_seen.add(handle)
                continue

            variants = raw_product.get("variants") or {}
            nodes = list(variants.get("nodes") or [])
            variant_info = variants.get("pageInfo") or {}
            variant_cursor = variant_info.get("endCursor")
            variant_pages_for_product = 1
            while variant_info.get("hasNextPage"):
                if not variant_cursor or variant_pages_for_product >= 100:
                    raise ScrapeError(f"invalid or excessive variant pagination: {handle}")
                variant_payload = _graphql_request(
                    VARIANT_PAGE_QUERY,
                    {"handle": handle, "after": variant_cursor},
                    timeout,
                    retries,
                )
                api_request_count += 1
                extra_variant_page_count += 1
                next_product = (variant_payload.get("data") or {}).get("product")
                if not next_product or next_product.get("handle") != handle:
                    raise ScrapeError(f"product disappeared during variant pagination: {handle}")
                next_variants = next_product.get("variants") or {}
                nodes.extend(next_variants.get("nodes") or [])
                variant_info = next_variants.get("pageInfo") or {}
                variant_cursor = variant_info.get("endCursor")
                variant_pages_for_product += 1
                if delay:
                    time.sleep(delay)
            raw_product["variants"]["nodes"] = nodes
            raw_product["variants"]["pageInfo"] = {"hasNextPage": False, "endCursor": variant_cursor}
            products.append(normalize_product(raw_product, synced_at))

        if not page_info.get("hasNextPage"):
            break
        next_cursor = page_info.get("endCursor")
        if not next_cursor or next_cursor == after:
            raise ScrapeError("invalid product pagination cursor")
        after = str(next_cursor)
        if delay:
            time.sleep(delay)

    products.sort(key=lambda item: item["product_number"])
    return products, {
        "synced_at": synced_at,
        "api_request_count": api_request_count,
        "product_page_count": product_page_count,
        "extra_variant_page_count": extra_variant_page_count,
        "storefront_product_count": len(seen_handles),
        "excluded_special_product_count": len(excluded_seen),
        "excluded_special_product_numbers": sorted(excluded_seen),
        "excluded_special_not_present_count": len(excluded_product_numbers - excluded_seen),
        "excluded_special_not_present_product_numbers": sorted(excluded_product_numbers - excluded_seen),
    }


def _change(
    changes: list[dict[str, Any]],
    detected_at: str,
    entity_type: str,
    change_type: str,
    stable_id: str,
    product_number: str,
    variant_sku: str | None = None,
    before: Any = None,
    after: Any = None,
) -> None:
    changes.append({
        "detected_at": detected_at,
        "entity_type": entity_type,
        "change_type": change_type,
        "stable_id": stable_id,
        "product_number": product_number,
        "variant_sku": variant_sku,
        "before": before,
        "after": after,
    })


def _variant_summary(variant: dict[str, Any]) -> dict[str, Any]:
    return {
        "active": variant["active"],
        "available_for_sale": variant["available_for_sale"],
        "tax_included_price_jpy": variant["tax_included_price_jpy"],
        "mini_program_price_jpy": variant["mini_program_price_jpy"],
        "color": variant["color"],
        "size": variant["size"],
        "variant_image": variant["variant_image"],
    }


def merge_catalog(
    previous_master: dict[str, Any] | None,
    current_products: list[dict[str, Any]],
    excluded_product_numbers: set[str],
    synced_at: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    previous_products = {
        item["product_number"]: copy.deepcopy(item)
        for item in (previous_master or {}).get("products", [])
        if item["product_number"] not in excluded_product_numbers
    }
    current_by_number = {item["product_number"]: copy.deepcopy(item) for item in current_products}
    if len(current_by_number) != len(current_products):
        raise ScrapeError("current catalog contains duplicate product numbers")
    leaked = sorted(set(current_by_number) & excluded_product_numbers)
    if leaked:
        raise ScrapeError(f"special products leaked into current catalog: {leaked}")

    changes: list[dict[str, Any]] = []
    merged_products: list[dict[str, Any]] = []
    product_metadata_fields = (
        "shopify_product_id",
        "handle",
        "name",
        "brand",
        "product_type",
        "category",
        "tags",
        "product_url",
    )
    variant_metadata_fields = (
        "shopify_variant_id",
        "title",
        "selected_options",
        "color",
        "size",
    )

    for product_number in sorted(current_by_number):
        current = current_by_number[product_number]
        old = previous_products.pop(product_number, None)
        if old is None:
            _change(changes, synced_at, "product", "new_product", product_number, product_number)
            for variant in current["variants"]:
                _change(
                    changes,
                    synced_at,
                    "variant",
                    "new_variant",
                    variant["stable_id"],
                    product_number,
                    variant["sku"],
                    after=_variant_summary(variant),
                )
            merged_products.append(current)
            continue

        current["first_seen_at"] = old.get("first_seen_at", synced_at)
        current["last_seen_at"] = synced_at
        current["inactivated_at"] = None
        if not old.get("active", True):
            _change(changes, synced_at, "product", "product_reactivated", product_number, product_number)
        before_metadata = {field: old.get(field) for field in product_metadata_fields}
        after_metadata = {field: current.get(field) for field in product_metadata_fields}
        if before_metadata != after_metadata:
            _change(
                changes,
                synced_at,
                "product",
                "product_metadata_changed",
                product_number,
                product_number,
                before=before_metadata,
                after=after_metadata,
            )
        before_images = {"main_image": old.get("main_image"), "color_images": old.get("color_images", [])}
        after_images = {"main_image": current.get("main_image"), "color_images": current.get("color_images", [])}
        if before_images != after_images:
            _change(
                changes,
                synced_at,
                "product",
                "product_images_changed",
                product_number,
                product_number,
                before=before_images,
                after=after_images,
            )

        old_variants = {item["stable_id"]: item for item in old.get("variants", [])}
        merged_variants: list[dict[str, Any]] = []
        for current_variant in current["variants"]:
            stable_id = current_variant["stable_id"]
            old_variant = old_variants.pop(stable_id, None)
            if old_variant is None:
                _change(
                    changes,
                    synced_at,
                    "variant",
                    "new_variant",
                    stable_id,
                    product_number,
                    current_variant["sku"],
                    after=_variant_summary(current_variant),
                )
                merged_variants.append(current_variant)
                continue
            current_variant["first_seen_at"] = old_variant.get("first_seen_at", synced_at)
            current_variant["last_seen_at"] = synced_at
            current_variant["inactivated_at"] = None
            if not old_variant.get("active", True):
                _change(
                    changes,
                    synced_at,
                    "variant",
                    "variant_reactivated",
                    stable_id,
                    product_number,
                    current_variant["sku"],
                )
            if old_variant.get("tax_included_price_jpy") != current_variant["tax_included_price_jpy"]:
                _change(
                    changes,
                    synced_at,
                    "variant",
                    "price_changed",
                    stable_id,
                    product_number,
                    current_variant["sku"],
                    before={
                        "tax_included_price_jpy": old_variant.get("tax_included_price_jpy"),
                        "mini_program_price_jpy": old_variant.get("mini_program_price_jpy"),
                    },
                    after={
                        "tax_included_price_jpy": current_variant["tax_included_price_jpy"],
                        "mini_program_price_jpy": current_variant["mini_program_price_jpy"],
                    },
                )
            if old_variant.get("available_for_sale") != current_variant["available_for_sale"]:
                _change(
                    changes,
                    synced_at,
                    "variant",
                    "inventory_changed",
                    stable_id,
                    product_number,
                    current_variant["sku"],
                    before=old_variant.get("available_for_sale"),
                    after=current_variant["available_for_sale"],
                )
            before_image = {
                "variant_image": old_variant.get("variant_image"),
                "resolved_image": old_variant.get("resolved_image"),
                "image_source": old_variant.get("image_source"),
            }
            after_image = {
                "variant_image": current_variant.get("variant_image"),
                "resolved_image": current_variant.get("resolved_image"),
                "image_source": current_variant.get("image_source"),
            }
            if before_image != after_image:
                _change(
                    changes,
                    synced_at,
                    "variant",
                    "variant_image_changed",
                    stable_id,
                    product_number,
                    current_variant["sku"],
                    before=before_image,
                    after=after_image,
                )
            before_variant_metadata = {field: old_variant.get(field) for field in variant_metadata_fields}
            after_variant_metadata = {field: current_variant.get(field) for field in variant_metadata_fields}
            if before_variant_metadata != after_variant_metadata:
                _change(
                    changes,
                    synced_at,
                    "variant",
                    "variant_metadata_changed",
                    stable_id,
                    product_number,
                    current_variant["sku"],
                    before=before_variant_metadata,
                    after=after_variant_metadata,
                )
            merged_variants.append(current_variant)

        for stable_id in sorted(old_variants):
            old_variant = old_variants[stable_id]
            if old_variant.get("active", True):
                old_variant["active"] = False
                old_variant["inactivated_at"] = synced_at
                _change(
                    changes,
                    synced_at,
                    "variant",
                    "variant_inactivated",
                    stable_id,
                    product_number,
                    old_variant.get("sku"),
                )
            merged_variants.append(old_variant)
        current["variants"] = sorted(merged_variants, key=lambda item: item["stable_id"])
        merged_products.append(current)

    for product_number in sorted(previous_products):
        old = previous_products[product_number]
        if old.get("active", True):
            old["active"] = False
            old["inactivated_at"] = synced_at
            _change(changes, synced_at, "product", "product_inactivated", product_number, product_number)
        for variant in old.get("variants", []):
            if variant.get("active", True):
                variant["active"] = False
                variant["inactivated_at"] = synced_at
                _change(
                    changes,
                    synced_at,
                    "variant",
                    "variant_inactivated",
                    variant["stable_id"],
                    product_number,
                    variant.get("sku"),
                )
        merged_products.append(old)

    merged_products.sort(key=lambda item: item["product_number"])
    change_counts = Counter(item["change_type"] for item in changes)
    master = {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "synced_at": synced_at,
        "identity": {
            "product": "product_number",
            "variant": "product_number::variant_sku",
        },
        "pricing": {
            "currency": "JPY",
            "mini_program_discount_rate": "0.65",
            "rounding": "ceiling_to_integer_jpy",
            "currency_conversion_applied": False,
        },
        "special_exclusion_count": len(excluded_product_numbers),
        "products": merged_products,
    }
    change_report = {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "detected_at": synced_at,
        "is_initial_sync": not bool((previous_master or {}).get("products")),
        "change_count": len(changes),
        "change_type_counts": dict(sorted(change_counts.items())),
        "changes": changes,
    }
    return master, change_report


def _json_cell(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _bool_cell(value: bool) -> str:
    return "true" if value else "false"


def product_rows(master: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    fields = [
        "product_number",
        "handle",
        "active",
        "name",
        "brand",
        "product_type",
        "category_id",
        "category_name",
        "tags_json",
        "product_url",
        "main_image_url",
        "main_image_width",
        "main_image_height",
        "color_images_json",
        "variant_count",
        "active_variant_count",
        "available_variant_count",
        "first_seen_at",
        "last_seen_at",
        "inactivated_at",
    ]
    rows = []
    for product in master["products"]:
        category = product.get("category") or {}
        image = product.get("main_image") or {}
        variants = product.get("variants", [])
        rows.append([
            product["product_number"],
            product["handle"],
            _bool_cell(product["active"]),
            product["name"],
            product["brand"],
            product["product_type"],
            category.get("id", ""),
            category.get("name", ""),
            _json_cell(product["tags"]),
            product["product_url"],
            image.get("url", ""),
            image.get("width", ""),
            image.get("height", ""),
            _json_cell(product["color_images"]),
            len(variants),
            sum(bool(item["active"]) for item in variants),
            sum(bool(item["active"] and item["available_for_sale"]) for item in variants),
            product["first_seen_at"],
            product["last_seen_at"],
            product.get("inactivated_at") or "",
        ])
    return fields, rows


def variant_rows(master: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    fields = [
        "stable_id",
        "product_number",
        "handle",
        "product_active",
        "product_name",
        "brand",
        "product_type",
        "category_name",
        "product_url",
        "sku",
        "variant_active",
        "available_for_sale",
        "variant_title",
        "selected_options_json",
        "color",
        "size",
        "tax_included_price_jpy",
        "mini_program_price_jpy",
        "variant_image_url",
        "resolved_image_url",
        "image_width",
        "image_height",
        "image_source",
        "first_seen_at",
        "last_seen_at",
        "inactivated_at",
    ]
    rows = []
    for product in master["products"]:
        category = product.get("category") or {}
        for variant in product.get("variants", []):
            variant_image = variant.get("variant_image") or {}
            resolved_image = variant.get("resolved_image") or {}
            rows.append([
                variant["stable_id"],
                product["product_number"],
                product["handle"],
                _bool_cell(product["active"]),
                product["name"],
                product["brand"],
                product["product_type"],
                category.get("name", ""),
                product["product_url"],
                variant["sku"],
                _bool_cell(variant["active"]),
                _bool_cell(variant["available_for_sale"]),
                variant["title"],
                _json_cell(variant["selected_options"]),
                variant["color"],
                variant["size"],
                variant["tax_included_price_jpy"],
                variant["mini_program_price_jpy"],
                variant_image.get("url", ""),
                resolved_image.get("url", ""),
                resolved_image.get("width", ""),
                resolved_image.get("height", ""),
                variant["image_source"],
                variant["first_seen_at"],
                variant["last_seen_at"],
                variant.get("inactivated_at") or "",
            ])
    return fields, rows


def change_rows(report: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    fields = [
        "detected_at",
        "entity_type",
        "change_type",
        "stable_id",
        "product_number",
        "variant_sku",
        "before_json",
        "after_json",
    ]
    return fields, [
        [
            item["detected_at"],
            item["entity_type"],
            item["change_type"],
            item["stable_id"],
            item["product_number"],
            item.get("variant_sku") or "",
            _json_cell(item.get("before")),
            _json_cell(item.get("after")),
        ]
        for item in report["changes"]
    ]


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _write_json(path: Path, value: Any) -> None:
    _write_text_atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _write_csv(path: Path, fields: list[str], rows: list[list[Any]]) -> None:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(fields)
    writer.writerows(rows)
    _write_text_atomic(path, "\ufeff" + stream.getvalue())


def _classification(product: dict[str, Any]) -> str | None:
    searchable = " ".join([
        product.get("name", ""),
        product.get("product_type", ""),
        " ".join(product.get("tags", [])),
    ])
    if is_footwear_product(product.get("name", ""), product.get("tags", [])):
        return "footwear"
    if any(marker in searchable for marker in ("ベビー", "スタイ", "ロンパース", "肌着", "おくるみ", "マグ", "哺乳")):
        return "baby"
    if any(marker in searchable for marker in ("シャツ", "パンツ", "ジャケット", "コート", "セーター", "トレーナー", "ワンピース", "スカート", "ウェア")):
        return "apparel"
    if any(marker in searchable for marker in ("バッグ", "タオル", "食器", "おもちゃ", "雑貨", "文具", "ボトル", "ケース", "ポーチ")):
        return "goods"
    return None


def build_validation_report(
    master: dict[str, Any],
    excluded_product_numbers: set[str],
    synced_at: str,
) -> dict[str, Any]:
    active_products = [item for item in master["products"] if item["active"]]
    leaks = sorted({item["product_number"] for item in active_products} & excluded_product_numbers)
    if leaks:
        raise ScrapeError(f"special products leaked into mini-program pool: {leaks}")
    examples: dict[str, list[dict[str, Any]]] = {kind: [] for kind in ("footwear", "apparel", "baby", "goods")}
    for product in active_products:
        kind = _classification(product)
        if kind is None or len(examples[kind]) >= 2:
            continue
        active_variants = [item for item in product["variants"] if item["active"]]
        if not active_variants or not any(item.get("resolved_image") for item in active_variants):
            continue
        mapped_colors = {
            item["color"]
            for item in product.get("color_images", [])
            if item.get("images")
        }
        checks = {
            "all_prices_match_ceil_65_percent": all(
                item["mini_program_price_jpy"]
                == calculate_mini_program_price_jpy(item["tax_included_price_jpy"])
                for item in active_variants
            ),
            "all_variants_have_sku": all(bool(item["sku"]) for item in active_variants),
            "all_variants_have_selected_options": all(bool(item["selected_options"]) for item in active_variants),
            "all_variants_have_color_and_size": all(bool(item["color"] and item["size"]) for item in active_variants),
            "all_variants_have_official_image": all(bool(item.get("resolved_image")) for item in active_variants),
            "all_colors_have_image_mapping": all(item["color"] in mapped_colors for item in active_variants),
        }
        sample_variants = []
        sampled_colors: set[str] = set()
        for item in active_variants:
            if item["color"] not in sampled_colors:
                sample_variants.append(item)
                sampled_colors.add(item["color"])
            if len(sample_variants) == 3:
                break
        if len(sample_variants) < 3:
            sample_ids = {item["stable_id"] for item in sample_variants}
            sample_variants.extend(
                item for item in active_variants if item["stable_id"] not in sample_ids
            )
            sample_variants = sample_variants[:3]
        examples[kind].append({
            "product_number": product["product_number"],
            "name": product["name"],
            "product_type": product["product_type"],
            "category": product["category"],
            "product_url": product["product_url"],
            "variant_count": len(active_variants),
            "color_count": len({item["color"] for item in active_variants}),
            "size_count": len({item["size"] for item in active_variants}),
            "sample_variants": [
                {
                    "sku": item["sku"],
                    "color": item["color"],
                    "size": item["size"],
                    "available_for_sale": item["available_for_sale"],
                    "tax_included_price_jpy": item["tax_included_price_jpy"],
                    "mini_program_price_jpy": item["mini_program_price_jpy"],
                    "image_url": (item.get("resolved_image") or {}).get("url"),
                }
                for item in sample_variants
            ],
            "checks": checks,
            "passed": all(checks.values()),
        })
    missing_types = [kind for kind, items in examples.items() if not items]
    failed_examples = [item["product_number"] for items in examples.values() for item in items if not item["passed"]]
    if missing_types or failed_examples:
        raise ScrapeError(
            f"cross-category validation failed; missing_types={missing_types}, failed_products={failed_examples}"
        )
    serialized = json.dumps(master, ensure_ascii=False).casefold()
    forbidden_conversion_fields = [term for term in ("pdf_price", "rmb", "cny", "exchange_rate", "0.0435") if term in serialized]
    if forbidden_conversion_fields:
        raise ScrapeError(f"currency-conversion fields leaked into catalog: {forbidden_conversion_fields}")
    return {
        "validated_at": synced_at,
        "special_exclusion_set_count": len(excluded_product_numbers),
        "special_products_in_active_pool": leaks,
        "currency": "JPY",
        "discount_rate": "0.65",
        "currency_conversion_applied": False,
        "forbidden_currency_conversion_fields": forbidden_conversion_fields,
        "examples": examples,
        "passed": True,
    }


def build_crawl_stats(
    master: dict[str, Any],
    fetch_stats: dict[str, Any],
    change_report: dict[str, Any],
    excluded_product_numbers: set[str],
) -> dict[str, Any]:
    products = master["products"]
    variants = [variant for product in products for variant in product.get("variants", [])]
    active_products = [item for item in products if item["active"]]
    active_variants = [item for item in variants if item["active"]]
    product_types = Counter(item.get("product_type") or "(blank)" for item in active_products)
    brands = Counter(item.get("brand") or "(blank)" for item in active_products)
    return {
        **fetch_stats,
        "schema_version": CATALOG_SCHEMA_VERSION,
        "special_exclusion_set_count": len(excluded_product_numbers),
        "master_product_count": len(products),
        "active_product_count": len(active_products),
        "mini_program_pool_product_count": len(active_products),
        "inactive_product_count": len(products) - len(active_products),
        "master_variant_count": len(variants),
        "active_variant_count": len(active_variants),
        "mini_program_pool_variant_count": len(active_variants),
        "inactive_variant_count": len(variants) - len(active_variants),
        "available_for_sale_variant_count": sum(bool(item["available_for_sale"]) for item in active_variants),
        "active_products_without_main_image": sum(not item.get("main_image") for item in active_products),
        "active_variants_without_any_image": sum(not item.get("resolved_image") for item in active_variants),
        "active_variants_without_variant_specific_image": sum(not item.get("variant_image") for item in active_variants),
        "product_type_counts": dict(sorted(product_types.items())),
        "brand_counts": dict(sorted(brands.items())),
        "incremental_change_count": change_report["change_count"],
        "incremental_change_type_counts": change_report["change_type_counts"],
    }


def run_catalog_sync(
    exclusions_path: Path,
    output_dir: Path,
    report_dir: Path | None = None,
    *,
    page_size: int = 100,
    delay: float = 0.1,
    timeout: float = 30,
    retries: int = 2,
    max_pages: int = 1000,
) -> dict[str, Any]:
    excluded = set(read_product_numbers(exclusions_path))
    if len(excluded) != 351:
        raise ValueError(f"special exclusion manifest must contain exactly 351 SKUs, got {len(excluded)}")
    master_path = output_dir / "master_catalog.json"
    previous = json.loads(master_path.read_text(encoding="utf-8")) if master_path.exists() else None
    current, fetch_stats = fetch_all_storefront_products(
        excluded,
        page_size=page_size,
        delay=delay,
        timeout=timeout,
        retries=retries,
        max_pages=max_pages,
    )
    synced_at = fetch_stats["synced_at"]
    master, changes = merge_catalog(previous, current, excluded, synced_at)
    validation = build_validation_report(master, excluded, synced_at)
    stats = build_crawl_stats(master, fetch_stats, changes, excluded)

    outputs = {
        "master_json": output_dir / "master_catalog.json",
        "products_csv": output_dir / "products.csv",
        "variants_csv": output_dir / "variants.csv",
        "changes_json": output_dir / "incremental_changes.json",
        "changes_csv": output_dir / "incremental_changes.csv",
        "stats_json": output_dir / "crawl_stats.json",
        "validation_json": output_dir / "validation_report.json",
    }
    _write_json(outputs["master_json"], master)
    product_fields, products = product_rows(master)
    _write_csv(outputs["products_csv"], product_fields, products)
    variant_fields, variants = variant_rows(master)
    _write_csv(outputs["variants_csv"], variant_fields, variants)
    _write_json(outputs["changes_json"], changes)
    change_fields, change_values = change_rows(changes)
    _write_csv(outputs["changes_csv"], change_fields, change_values)
    _write_json(outputs["validation_json"], validation)

    stats["output_file_size_bytes"] = {
        name: path.stat().st_size
        for name, path in outputs.items()
        if name != "stats_json"
    }
    stats["master_sha256"] = hashlib.sha256(outputs["master_json"].read_bytes()).hexdigest()
    stats["output_paths"] = {name: str(path) for name, path in outputs.items()}
    _write_json(outputs["stats_json"], stats)

    if report_dir:
        _write_json(report_dir / "crawl_stats.json", stats)
        tracked_change_summary = {
            key: value for key, value in changes.items() if key != "changes"
        }
        tracked_change_summary.update({
            "full_report_path": str(outputs["changes_json"]),
            "full_report_size_bytes": outputs["changes_json"].stat().st_size,
            "full_report_sha256": hashlib.sha256(outputs["changes_json"].read_bytes()).hexdigest(),
            "full_csv_path": str(outputs["changes_csv"]),
            "full_csv_size_bytes": outputs["changes_csv"].stat().st_size,
            "sample_changes": changes["changes"][:20],
        })
        _write_json(report_dir / "incremental_changes.json", tracked_change_summary)
        _write_json(report_dir / "validation_report.json", validation)

    result = {
        "synced_at": synced_at,
        "outputs": {name: str(path) for name, path in outputs.items()},
        "master_sha256": stats["master_sha256"],
        "stats": stats,
        "validation_passed": validation["passed"],
    }
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build and incrementally update the MIKI HOUSE Storefront master catalog")
    parser.add_argument("--exclusions", default="special_skus_2026aw.csv")
    parser.add_argument("--output", default="output/storefront-master")
    parser.add_argument("--report-dir", default="deliverables/storefront_catalog")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--delay", type=float, default=0.1)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--max-pages", type=int, default=1000)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_catalog_sync(
            Path(args.exclusions),
            Path(args.output),
            Path(args.report_dir) if args.report_dir else None,
            page_size=args.page_size,
            delay=args.delay,
            timeout=args.timeout,
            retries=args.retries,
            max_pages=args.max_pages,
        )
    except (OSError, ValueError, ScrapeError) as exc:
        raise SystemExit(f"catalog sync failed: {exc}") from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
