from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import html
import json
import re
import unicodedata
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .catalog import (
    CATALOG_SCHEMA_VERSION,
    _write_csv,
    _write_json,
    fetch_all_storefront_products,
    merge_catalog,
    variant_rows,
)
from .csv_input import read_product_numbers
from .scraper import ScrapeError


STABLE_CATALOG_SCHEMA_VERSION = 2
STABLE = "STABLE"
EXCLUDED = "EXCLUDED"
REVIEW_REQUIRED = "REVIEW_REQUIRED_STABILITY"
PDF_SPECIAL = "PDF_SPECIAL_LIST"
WEB_EXCLUSIVE = "WEB_EXCLUSIVE"
LIMITED_TIME_PRICE = "LIMITED_TIME_PRICE"
NON_SELLABLE_SERVICE_OR_ADDON = "NON_SELLABLE_SERVICE_OR_ADDON"

# These values come from the official Storefront productType field.  They are
# cart-support entities, fees, free novelties, or message/wrapping choices, not
# independent retail merchandise.  The rule deliberately does not infer this
# classification from a zero price alone.
_NON_SELLABLE_PRODUCT_TYPE_RULES = {
    "名入れ代商品": {
        "kind": "PERSONALIZATION_FEE",
        "corroborating_tags": {"名入れ代", "手数料商品"},
    },
    "ノベルティ商品": {
        "kind": "NOVELTY_OR_FREE_ITEM",
        "corroborating_tags": {"ノベルティ商品"},
    },
    "メッセージカード商品": {
        "kind": "MESSAGE_CARD_ADDON",
        "corroborating_tags": {"メッセージカード", "手数料商品"},
    },
    "ギフトラッピング商品": {
        "kind": "GIFT_WRAPPING_ADDON",
        "corroborating_tags": {"ギフトラッピング商品", "手数料商品"},
    },
}
PERMANENT_NON_SELLABLE_PRODUCT_NUMBERS = frozenset({
    "00-9999-002",
    "00-9999-004",
    "00-9999-006",
    "00-9999-008",
    "00-9999-010",
    "00-9999-012",
    "00-9999-014",
    "19-1680-494",
    "19-1683-493",
    "19-1685-499",
    "19-1685-680",
    "19-1689-682",
    "19-1693-681",
    "19-1714-687",
    "19-1716-140",
    "19-1986-680",
    "29-1034-571",
    "49-1029-737",
    "49-1040-956",
    "49-1043-788",
    "69-1085-148",
    "99-9999-000",
    "99-9999-118",
    "99-9999-119",
    "99-9999-120",
    "99-9999-121",
    "99-9999-122",
})

_WEB_EXCLUSIVE_RE = re.compile(
    r"(?:web(?:\s*(?:shop|store|通販))?\s*限定|web\s*limited|weblimited|"
    r"オンライン(?:ショップ)?\s*限定|ウェブ\s*限定|online\s*exclusive)",
    re.IGNORECASE,
)
_LIMITED_PRICE_RE = re.compile(
    r"(?:期間\s*限定\s*(?:価格|プライス)|限定\s*価格|特別\s*価格|"
    r"limited[\s_-]*time[\s_-]*(?:price|offer)|(?:sale|セール)(?:\s*価格)?)",
    re.IGNORECASE,
)
_AMBIGUOUS_WEB_TAGS = frozenset({"webitem", "webアイテム"})
_AMBIGUOUS_LIMITED_RE = re.compile(r"期間\s*限定|キャンペーン|値下げ", re.IGNORECASE)
_POTENTIAL_UNSTABLE_RE = re.compile(r"予約|受注|オーダー|pre[\s_-]*order", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalized(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def _search_fields(product: dict[str, Any]) -> dict[str, str]:
    return {
        "name": str(product.get("name") or ""),
        "product_type": str(product.get("product_type") or ""),
        "tags": "\n".join(str(value) for value in product.get("tags") or []),
        "description": str(product.get("description") or ""),
        "description_html": str(product.get("description_html") or ""),
    }


def _matching_fields(fields: dict[str, str], pattern: re.Pattern[str]) -> list[str]:
    return [name for name, value in fields.items() if pattern.search(_normalized(value))]


def _evidence(reason: str, fields: Iterable[str], signal: str) -> dict[str, Any]:
    return {
        "reason": reason,
        "matched_fields": sorted(set(fields)),
        "signal": signal,
    }


def _non_sellable_service_or_addon_evidence(
    product: dict[str, Any],
) -> dict[str, Any] | None:
    product_number = str(product.get("product_number") or product.get("handle") or "").strip()
    product_type = str(product.get("product_type") or "").strip()
    rule = _NON_SELLABLE_PRODUCT_TYPE_RULES.get(product_type)
    permanently_confirmed = product_number in PERMANENT_NON_SELLABLE_PRODUCT_NUMBERS
    if rule is None and not permanently_confirmed:
        return None
    if rule is None:
        return {
            "reason": NON_SELLABLE_SERVICE_OR_ADDON,
            "matched_fields": ["product_number"],
            "signal": "permanent_officially_audited_non_sellable_product_number",
            "service_or_addon_kind": "PERMANENTLY_CONFIRMED_SERVICE_OR_ADDON",
            "official_product_type": product_type,
            "corroborating_tags": [],
            "corroboration_status": "PERMANENT_PRODUCT_NUMBER_MANIFEST",
            "classification_does_not_depend_on_zero_price": True,
        }
    tags = {str(value).strip() for value in product.get("tags") or [] if str(value).strip()}
    corroborating = sorted(tags & set(rule["corroborating_tags"]))
    if not corroborating:
        # An exact cart-support productType is strong enough to prevent a sale,
        # but missing its normal corroborating tag is retained for auditability.
        corroboration_status = "OFFICIAL_PRODUCT_TYPE_ONLY"
    else:
        corroboration_status = "OFFICIAL_PRODUCT_TYPE_AND_TAG"
    return {
        "reason": NON_SELLABLE_SERVICE_OR_ADDON,
        "matched_fields": ["product_type", *(["tags"] if corroborating else [])],
        "signal": "explicit_official_cart_support_product_type",
        "service_or_addon_kind": rule["kind"],
        "official_product_type": product_type,
        "corroborating_tags": corroborating,
        "corroboration_status": corroboration_status,
        "permanent_product_number_match": permanently_confirmed,
        "classification_does_not_depend_on_zero_price": True,
    }


def assess_product_stability(
    product: dict[str, Any], special_product_numbers: set[str]
) -> dict[str, Any]:
    """Classify a product before every Shijiu planning or live-write stage.

    Evidence is deliberately limited to explicit Storefront fields. Absence of a
    promotional marker is not inferred from a missing product, failed crawl, or
    partial response; those conditions are rejected by the catalog crawler.
    """
    number = str(product.get("product_number") or product.get("handle") or "").strip()
    if not number:
        return {
            "status": REVIEW_REQUIRED,
            "excluded_reason": REVIEW_REQUIRED,
            "evidence": [_evidence(REVIEW_REQUIRED, ["product_number"], "missing_product_number")],
        }
    if number in special_product_numbers:
        return {
            "status": EXCLUDED,
            "excluded_reason": PDF_SPECIAL,
            "evidence": [_evidence(PDF_SPECIAL, ["product_number"], "exact_manifest_match")],
        }

    non_sellable_evidence = _non_sellable_service_or_addon_evidence(product)
    if non_sellable_evidence is not None:
        return {
            "status": EXCLUDED,
            "excluded_reason": NON_SELLABLE_SERVICE_OR_ADDON,
            "evidence": [non_sellable_evidence],
        }

    fields = _search_fields(product)
    web_fields = _matching_fields(fields, _WEB_EXCLUSIVE_RE)
    if web_fields:
        return {
            "status": EXCLUDED,
            "excluded_reason": WEB_EXCLUSIVE,
            "evidence": [_evidence(WEB_EXCLUSIVE, web_fields, "explicit_web_exclusive_marker")],
        }

    limited_fields = _matching_fields(fields, _LIMITED_PRICE_RE)
    structured_discounts: list[dict[str, Any]] = []
    structured_anomalies: list[dict[str, Any]] = []
    for variant in product.get("variants") or []:
        compare_at = variant.get("compare_at_price_jpy")
        current = variant.get("tax_included_price_jpy")
        if compare_at is None:
            continue
        try:
            compare_at_int, current_int = int(compare_at), int(current)
        except (TypeError, ValueError):
            structured_anomalies.append({"sku": str(variant.get("sku") or ""), "issue": "invalid_compare_at"})
            continue
        if compare_at_int > current_int:
            structured_discounts.append({
                "sku": str(variant.get("sku") or ""),
                "current_price_jpy": current_int,
                "compare_at_price_jpy": compare_at_int,
            })
        elif compare_at_int < current_int:
            structured_anomalies.append({
                "sku": str(variant.get("sku") or ""),
                "issue": "compare_at_below_current_price",
            })
    if limited_fields or structured_discounts:
        evidence = []
        if limited_fields:
            evidence.append(_evidence(
                LIMITED_TIME_PRICE, limited_fields, "explicit_limited_or_promotional_price_marker"
            ))
        if structured_discounts:
            evidence.append({
                "reason": LIMITED_TIME_PRICE,
                "matched_fields": ["variants.compare_at_price_jpy", "variants.tax_included_price_jpy"],
                "signal": "compare_at_price_above_current_price",
                "affected_variant_count": len(structured_discounts),
                "affected_variant_skus": sorted(row["sku"] for row in structured_discounts),
            })
        return {
            "status": EXCLUDED,
            "excluded_reason": LIMITED_TIME_PRICE,
            "evidence": evidence,
        }

    tags = {_normalized(value) for value in product.get("tags") or []}
    ambiguous_web = sorted(tags & _AMBIGUOUS_WEB_TAGS)
    ambiguous_limited_fields = _matching_fields(fields, _AMBIGUOUS_LIMITED_RE)
    if ambiguous_web or ambiguous_limited_fields or structured_anomalies:
        evidence = []
        if ambiguous_web:
            evidence.append(_evidence(
                REVIEW_REQUIRED, ["tags"], "ambiguous_web_channel_tag_without_exclusive_marker"
            ))
        if ambiguous_limited_fields:
            evidence.append(_evidence(
                REVIEW_REQUIRED,
                ambiguous_limited_fields,
                "limited_or_campaign_marker_without_explicit_price_evidence",
            ))
        if structured_anomalies:
            evidence.append({
                "reason": REVIEW_REQUIRED,
                "matched_fields": ["variants.compare_at_price_jpy"],
                "signal": "invalid_or_inverted_compare_at_price",
                "affected_variant_skus": sorted(row["sku"] for row in structured_anomalies),
            })
        return {
            "status": REVIEW_REQUIRED,
            "excluded_reason": REVIEW_REQUIRED,
            "evidence": evidence,
        }

    unsafe_image_urls = []
    for entry in product.get("ordered_images") or []:
        url = str(((entry.get("image") or {}).get("url")) or "").strip()
        if not url:
            continue
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme.casefold() != "https" or not parsed.hostname:
            unsafe_image_urls.append(url)
    if unsafe_image_urls:
        return {
            "status": REVIEW_REQUIRED,
            "excluded_reason": REVIEW_REQUIRED,
            "evidence": [{
                "reason": REVIEW_REQUIRED,
                "matched_fields": ["ordered_images.image.url"],
                "signal": "non_https_or_unqualified_official_image_resource",
                "affected_resource_count": len(unsafe_image_urls),
                "source_url_sha256": sorted(
                    hashlib.sha256(url.encode("utf-8")).hexdigest() for url in unsafe_image_urls
                ),
            }],
        }


    variants = [row for row in product.get("variants") or [] if row.get("active", True)]
    quality_evidence: list[dict[str, Any]] = []
    if not variants:
        quality_evidence.append({
            "reason": REVIEW_REQUIRED,
            "matched_fields": ["variants"],
            "signal": "missing_active_sellable_variant",
        })
    else:
        missing_sku = sorted(
            str(row.get("stable_id") or "") for row in variants if not str(row.get("sku") or "").strip()
        )
        if missing_sku:
            quality_evidence.append({
                "reason": REVIEW_REQUIRED,
                "matched_fields": ["variants.sku"],
                "signal": "missing_stable_variant_sku",
                "affected_variant_count": len(missing_sku),
            })
        invalid_prices = []
        for row in variants:
            try:
                price = int(row.get("tax_included_price_jpy"))
            except (TypeError, ValueError):
                price = 0
            if price <= 0:
                invalid_prices.append(str(row.get("sku") or ""))
        if invalid_prices:
            quality_evidence.append({
                "reason": REVIEW_REQUIRED,
                "matched_fields": ["variants.tax_included_price_jpy"],
                "signal": "zero_or_invalid_sellable_price_requires_review",
                "affected_variant_count": len(invalid_prices),
                "affected_variant_skus": sorted(invalid_prices),
                "not_automatically_classified_as_service_from_price": True,
            })

    missing_media_fields: list[str] = []
    if not str((product.get("main_image") or {}).get("url") or "").strip():
        missing_media_fields.append("main_image.url")
    ordered_images = product.get("ordered_images") or []
    if not ordered_images or any(
        not str(((row.get("image") or {}).get("url")) or "").strip()
        for row in ordered_images
    ):
        missing_media_fields.append("ordered_images.image.url")
    if variants and any(
        not str((row.get("resolved_image") or {}).get("url") or "").strip()
        for row in variants
    ):
        missing_media_fields.append("variants.resolved_image.url")
    if missing_media_fields:
        quality_evidence.append({
            "reason": REVIEW_REQUIRED,
            "matched_fields": sorted(missing_media_fields),
            "signal": "missing_required_sellable_media",
            "replacement_with_other_product_image_forbidden": True,
        })
    if quality_evidence:
        return {
            "status": REVIEW_REQUIRED,
            "excluded_reason": REVIEW_REQUIRED,
            "evidence": quality_evidence,
        }

    return {"status": STABLE, "excluded_reason": None, "evidence": []}


def shijiu_good_details(product: dict[str, Any], maximum_characters: int = 1024) -> str:
    text = html.unescape(str(product.get("description") or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = _URL_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        text = str(product.get("name") or "").strip()
    escaped = html.escape(text)
    prefix, suffix = "<p>", "</p>"
    maximum_body = max(0, maximum_characters - len(prefix) - len(suffix))
    result = prefix + escaped[:maximum_body] + suffix
    if re.search(r"<img\b|https?://", result, flags=re.IGNORECASE):
        raise ScrapeError("stable good_details contains a prohibited image or URL")
    return result


def _image_resources(product: dict[str, Any]) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in product.get("ordered_images") or []:
        image = entry.get("image") or {}
        url = str(image.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        descriptor = {
            "order": len(resources) + 1,
            "role": str(entry.get("role") or "product_gallery"),
            "source_url": url,
            "source_width": image.get("width"),
            "source_height": image.get("height"),
            "alt_text": str(image.get("alt_text") or ""),
            "colors": entry.get("colors") or [],
            "variant_skus": entry.get("variant_skus") or [],
        }
        descriptor["source_url_sha256"] = hashlib.sha256(url.encode("utf-8")).hexdigest()
        descriptor["resource_descriptor_sha256"] = _canonical_sha256(descriptor)
        resources.append(descriptor)
    return resources


def prepare_stable_product(product: dict[str, Any], captured_at: str) -> dict[str, Any]:
    result = copy.deepcopy(product)
    result["stability"] = {
        "status": STABLE,
        "excluded_reason": None,
        "evaluated_at": captured_at,
        "policy_version": STABLE_CATALOG_SCHEMA_VERSION,
    }
    result["source_captured_at"] = captured_at
    result["shijiu_good_details"] = shijiu_good_details(result)
    result["image_resources"] = _image_resources(result)
    hashable = copy.deepcopy(result)
    for key in ("first_seen_at", "last_seen_at", "source_captured_at", "source_content_sha256"):
        hashable.pop(key, None)
    if isinstance(hashable.get("stability"), dict):
        hashable["stability"].pop("evaluated_at", None)
    for variant in hashable.get("variants") or []:
        for key in ("first_seen_at", "last_seen_at"):
            variant.pop(key, None)
    result["source_content_sha256"] = _canonical_sha256(hashable)
    return result


def partition_stable_catalog(
    products: list[dict[str, Any]], special_product_numbers: set[str], captured_at: str
) -> dict[str, Any]:
    stable: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    potential_labels: list[dict[str, Any]] = []
    for product in products:
        decision = assess_product_stability(product, special_product_numbers)
        number = str(product.get("product_number") or "")
        fields = _search_fields(product)
        potential_fields = _matching_fields(fields, _POTENTIAL_UNSTABLE_RE)
        if potential_fields:
            potential_labels.append({
                "product_number": number,
                "name": product.get("name") or "",
                "matched_fields": potential_fields,
                "action": "STATISTICS_ONLY_NOT_EXCLUDED",
            })
        if decision["status"] == STABLE:
            stable.append(prepare_stable_product(product, captured_at))
            continue
        row = {
            "product_number": number,
            "name": str(product.get("name") or ""),
            "status": decision["status"],
            "excluded_reason": decision["excluded_reason"],
            "evidence": decision["evidence"],
            "product_url": str(product.get("product_url") or ""),
        }
        (review if decision["status"] == REVIEW_REQUIRED else excluded).append(row)
    return {
        "stable_products": sorted(stable, key=lambda row: row["product_number"]),
        "excluded_products": sorted(excluded, key=lambda row: row["product_number"]),
        "review_required": sorted(review, key=lambda row: row["product_number"]),
        "potential_unstable_labels": sorted(potential_labels, key=lambda row: row["product_number"]),
    }


def _write_gzip_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=9) as stream:
        json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
        stream.write("\n")
    temporary.replace(path)


def _exclusion_csv_rows(rows: list[dict[str, Any]]) -> tuple[list[str], list[list[Any]]]:
    fields = ["product_number", "name", "status", "excluded_reason", "evidence_json", "product_url"]
    values = [[
        row["product_number"], row["name"], row["status"], row["excluded_reason"],
        json.dumps(row["evidence"], ensure_ascii=False, separators=(",", ":")), row["product_url"],
    ] for row in rows]
    return fields, values


def _stable_product_rows(products: list[dict[str, Any]]) -> tuple[list[str], list[list[Any]]]:
    fields = [
        "product_number", "name", "brand", "product_type", "product_url", "variant_count",
        "available_variant_count", "image_resource_count", "source_content_sha256",
    ]
    rows = [[
        product["product_number"], product["name"], product.get("brand") or "",
        product.get("product_type") or "", product.get("product_url") or "",
        len(product.get("variants") or []),
        sum(bool(row.get("available_for_sale")) for row in product.get("variants") or []),
        len(product.get("image_resources") or []), product["source_content_sha256"],
    ] for product in products]
    return fields, rows


def build_stability_audit(
    source_products: list[dict[str, Any]],
    partition: dict[str, Any],
    special_product_numbers: set[str],
    old_pool_numbers: set[str],
    fetch_stats: dict[str, Any],
) -> dict[str, Any]:
    stable = partition["stable_products"]
    excluded = partition["excluded_products"]
    review = partition["review_required"]
    by_reason: dict[str, list[dict[str, Any]]] = {}
    for row in excluded:
        by_reason.setdefault(row["excluded_reason"], []).append(row)
    current_numbers = {row["product_number"] for row in source_products}
    stable_numbers = {row["product_number"] for row in stable}
    removed = sorted(old_pool_numbers - stable_numbers)
    decision_by_number = {row["product_number"]: row for row in [*excluded, *review]}
    removed_rows = [{
        "product_number": number,
        "reason": (decision_by_number.get(number) or {}).get("excluded_reason", "NO_LONGER_ON_STOREFRONT"),
    } for number in removed]
    fields_with_web = {
        row["product_number"] for row in source_products
        if _matching_fields(_search_fields(row), _WEB_EXCLUSIVE_RE)
    }
    names_with_web = {
        row["product_number"] for row in source_products
        if _WEB_EXCLUSIVE_RE.search(_normalized(row.get("name")))
    }
    names_with_period_limited_price = {
        row["product_number"] for row in source_products
        if re.search(r"期間\s*限定\s*(?:価格|プライス)", _normalized(row.get("name")))
    }
    fields_with_limited_price = {
        row["product_number"] for row in source_products
        if _matching_fields(_search_fields(row), _LIMITED_PRICE_RE)
        or any(
            variant.get("compare_at_price_jpy") is not None
            and int(variant["compare_at_price_jpy"]) > int(variant["tax_included_price_jpy"])
            for variant in row.get("variants") or []
        )
    }
    stable_image_hosts = Counter()
    stable_non_https_images: list[str] = []
    for product in stable:
        for resource in product.get("image_resources") or []:
            parsed = urllib.parse.urlparse(resource["source_url"])
            stable_image_hosts[parsed.hostname or "(missing)"] += 1
            if parsed.scheme.casefold() != "https":
                stable_non_https_images.append(resource["source_url"])
    leaked_web = sorted(fields_with_web & stable_numbers)
    leaked_limited = sorted(fields_with_limited_price & stable_numbers)
    non_sellable_source_numbers = {
        row["product_number"]
        for row in source_products
        if _non_sellable_service_or_addon_evidence(row) is not None
    }
    leaked_non_sellable = sorted(non_sellable_source_numbers & stable_numbers)
    zero_or_invalid_price_leaks = sorted(
        row["product_number"]
        for row in stable
        if any(
            int(variant.get("tax_included_price_jpy") or 0) <= 0
            for variant in row.get("variants") or []
        )
    )
    missing_media_leaks = sorted(
        row["product_number"]
        for row in stable
        if not str((row.get("main_image") or {}).get("url") or "").strip()
        or not row.get("ordered_images")
        or any(
            not str((variant.get("resolved_image") or {}).get("url") or "").strip()
            for variant in row.get("variants") or []
        )
    )
    if (
        leaked_web
        or leaked_limited
        or leaked_non_sellable
        or zero_or_invalid_price_leaks
        or missing_media_leaks
        or stable_numbers & special_product_numbers
    ):
        raise ScrapeError("stability exclusion validation failed")
    counts = Counter(row["excluded_reason"] for row in excluded)
    return {
        "schema_version": STABLE_CATALOG_SCHEMA_VERSION,
        "generated_at": fetch_stats["synced_at"],
        "status": "COMPLETED_READ_ONLY_STOREFRONT",
        "source": "MIKIHOUSE_STOREFRONT",
        "target_write_status": "NOT_ATTEMPTED_PROHIBITED_CONCURRENT_WAWU_WRITER",
        "shijiu_safety": {
            "operator_reported_concurrent_writer": "WAWU",
            "writer_mutex_evidence_generated": False,
            "shijiu_read_requests": 0,
            "shijiu_create_requests": 0,
            "shijiu_update_requests": 0,
            "shijiu_cos_upload_requests": 0,
            "shijiu_shelf_or_inventory_or_price_writes": 0,
            "legacy_286_touched": False,
        },
        "historical_pilot_plan": {
            "path": "deliverables/shijiu_import/richtext_e2e_next_20_frozen_plan.json",
            "status": "STALE_BUSINESS_RULE_CHANGED",
            "historical_file_sha256_before_invalidation": "7a4cd2d2e8e351b84e5638f3fa81097211ee785ed9a6adb0eec373f29890820d",
            "business_rule_conflicts": [
                {"sequence": 17, "product_number": "14-9909-491", "excluded_reason": WEB_EXCLUSIVE}
            ],
            "executable": False,
            "replacement_20_product_plan_generated": False,
        },
        "counts": {
            "storefront_total_product_count": len(source_products),
            "pdf_special_list_manifest_count": len(special_product_numbers),
            "pdf_special_list_online_excluded_count": counts.get(PDF_SPECIAL, 0),
            "pdf_special_list_offline_remembered_count": len(special_product_numbers - current_numbers),
            "web_exclusive_excluded_count": counts.get(WEB_EXCLUSIVE, 0),
            "limited_time_price_excluded_count": counts.get(LIMITED_TIME_PRICE, 0),
            "non_sellable_service_or_addon_excluded_count": counts.get(
                NON_SELLABLE_SERVICE_OR_ADDON, 0
            ),
            "review_required_stability_count": len(review),
            "stable_catalog_product_count": len(stable),
            "stable_catalog_variant_count": sum(len(row.get("variants") or []) for row in stable),
            "stable_catalog_image_resource_count": sum(len(row.get("image_resources") or []) for row in stable),
        },
        "excluded": {
            PDF_SPECIAL: by_reason.get(PDF_SPECIAL, []),
            WEB_EXCLUSIVE: by_reason.get(WEB_EXCLUSIVE, []),
            LIMITED_TIME_PRICE: by_reason.get(LIMITED_TIME_PRICE, []),
            NON_SELLABLE_SERVICE_OR_ADDON: by_reason.get(
                NON_SELLABLE_SERVICE_OR_ADDON, []
            ),
            REVIEW_REQUIRED: review,
        },
        "potential_unstable_labels_not_excluded": partition["potential_unstable_labels"],
        "old_candidate_pool_comparison": {
            "historical_reported_candidate_counts": [2615, 2608],
            "stable_count_delta_vs_2615": len(stable_numbers) - 2615,
            "stable_count_delta_vs_2608": len(stable_numbers) - 2608,
            "actual_old_active_pool_count": len(old_pool_numbers),
            "removed_from_old_pool_count": len(removed_rows),
            "removed_from_old_pool": removed_rows,
            "new_to_stable_pool_count": len(stable_numbers - old_pool_numbers),
            "new_to_stable_pool_product_numbers": sorted(stable_numbers - old_pool_numbers),
        },
        "explicit_signal_coverage": {
            "web_exclusive_signal_product_count": len(fields_with_web),
            "product_name_web_exclusive_signal_count": len(names_with_web),
            "product_name_web_exclusive_signal_product_numbers": sorted(names_with_web),
            "product_name_web_exclusive_signal_products_in_stable_catalog": sorted(names_with_web & stable_numbers),
            "web_exclusive_signal_products_in_stable_catalog": leaked_web,
            "web_exclusive_exclusion_passed": not leaked_web,
            "limited_price_signal_product_count": len(fields_with_limited_price),
            "product_name_period_limited_price_signal_count": len(names_with_period_limited_price),
            "product_name_period_limited_price_signal_product_numbers": sorted(names_with_period_limited_price),
            "product_name_period_limited_price_signal_products_in_stable_catalog": sorted(names_with_period_limited_price & stable_numbers),
            "limited_price_signal_products_in_stable_catalog": leaked_limited,
            "limited_price_exclusion_passed": not leaked_limited,
            "non_sellable_service_or_addon_signal_product_count": len(
                non_sellable_source_numbers
            ),
            "non_sellable_service_or_addon_product_numbers": sorted(
                non_sellable_source_numbers
            ),
            "non_sellable_service_or_addon_products_in_stable_catalog": leaked_non_sellable,
            "non_sellable_service_or_addon_exclusion_passed": not leaked_non_sellable,
            "zero_or_invalid_price_products_in_stable_catalog": zero_or_invalid_price_leaks,
            "missing_required_media_products_in_stable_catalog": missing_media_leaks,
            "sellable_quality_guard_passed": not zero_or_invalid_price_leaks
            and not missing_media_leaks,
            "pdf_special_products_in_stable_catalog": sorted(stable_numbers & special_product_numbers),
            "all_required_exclusions_passed": not leaked_web
            and not leaked_limited
            and not leaked_non_sellable
            and not zero_or_invalid_price_leaks
            and not missing_media_leaks
            and not (stable_numbers & special_product_numbers),
        },
        "crawl": fetch_stats,
        "storefront_contract": {
            "product_fields_inspected": [
                "title", "productType", "tags", "description", "descriptionHtml",
                "featuredImage", "images", "media",
            ],
            "variant_fields_inspected": [
                "sku", "availableForSale", "selectedOptions", "image", "price", "compareAtPrice",
            ],
            "compare_at_price_live_contract_confirmed": True,
            "complete_product_variant_image_media_pagination_required": True,
        },
        "image_hash_semantics": {
            "source_content_downloaded_this_round": False,
            "source_url_sha256": "sha256 of exact official source URL",
            "resource_descriptor_sha256": "sha256 of ordered normalized resource descriptor",
            "product_source_content_sha256": "sha256 of normalized nonvolatile product data",
            "shijiu_cos_upload_count": 0,
            "stable_source_image_domain_counts": dict(sorted(stable_image_hosts.items())),
            "stable_non_https_image_count": len(stable_non_https_images),
        },
    }


def run_stable_catalog_sync(
    special_path: Path,
    output_dir: Path,
    report_dir: Path,
    *,
    old_master_path: Path | None = None,
    page_size: int = 100,
    delay: float = 0.1,
    timeout: float = 30,
    retries: int = 2,
    max_pages: int = 1000,
) -> dict[str, Any]:
    special = set(read_product_numbers(special_path))
    if len(special) != 351:
        raise ValueError(f"special exclusion manifest must contain exactly 351 SKUs, got {len(special)}")
    previous_pool = set()
    if old_master_path and old_master_path.exists():
        previous = json.loads(old_master_path.read_text(encoding="utf-8"))
        previous_pool = {
            str(row["product_number"]) for row in previous.get("products") or [] if row.get("active")
        }

    source_products, fetch_stats = fetch_all_storefront_products(
        set(), page_size=page_size, delay=delay, timeout=timeout, retries=retries,
        max_pages=max_pages,
    )
    captured_at = fetch_stats["synced_at"]
    partition = partition_stable_catalog(source_products, special, captured_at)
    stable_current = partition["stable_products"]
    unstable_numbers = {
        row["product_number"]
        for row in [*partition["excluded_products"], *partition["review_required"]]
    }
    stable_path = output_dir / "stable_catalog.json"
    previous_stable = json.loads(stable_path.read_text(encoding="utf-8")) if stable_path.exists() else None
    master, changes = merge_catalog(previous_stable, stable_current, unstable_numbers, captured_at)
    master.update({
        "schema_version": CATALOG_SCHEMA_VERSION,
        "catalog_kind": "MIKIHOUSE_STABLE_REGULAR_PRODUCT_POOL",
        "stability_policy_version": STABLE_CATALOG_SCHEMA_VERSION,
        "pdf_special_exclusion_count": len(special),
        "shijiu_action_source_required": "stable_catalog",
    })
    excluded_by_reason: dict[str, list[str]] = {}
    for row in partition["excluded_products"]:
        excluded_by_reason.setdefault(row["excluded_reason"], []).append(row["product_number"])
    online_special = sorted(excluded_by_reason.get(PDF_SPECIAL, []))
    master["special_exclusion"] = {
        "excluded_reason": PDF_SPECIAL,
        "policy": "permanent_pre_filter_for_all_shijiu_stages",
        "total_count": len(special),
        "online_excluded_count": len(online_special),
        "offline_remembered_count": len(special - set(online_special)),
        "online_product_numbers": online_special,
        "offline_product_numbers": sorted(special - set(online_special)),
    }
    master["stability_exclusion"] = {
        "policy": "pre_filter_before_all_shijiu_planning_and_live_stages",
        "web_exclusive_product_numbers": sorted(excluded_by_reason.get(WEB_EXCLUSIVE, [])),
        "limited_time_price_product_numbers": sorted(excluded_by_reason.get(LIMITED_TIME_PRICE, [])),
        "non_sellable_service_or_addon_product_numbers": sorted(
            excluded_by_reason.get(NON_SELLABLE_SERVICE_OR_ADDON, [])
        ),
        "review_required_product_numbers": sorted(
            row["product_number"] for row in partition["review_required"]
        ),
    }
    audit = build_stability_audit(source_products, partition, special, previous_pool, fetch_stats)
    master["stability_audit_summary"] = copy.deepcopy(audit["counts"])

    source_snapshot = {
        "schema_version": STABLE_CATALOG_SCHEMA_VERSION,
        "catalog_kind": "MIKIHOUSE_COMPLETE_STOREFRONT_SOURCE_SNAPSHOT",
        "captured_at": captured_at,
        "complete_pagination_validated": True,
        "products": source_products,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "source_catalog.json", source_snapshot)
    _write_json(stable_path, master)
    _write_json(output_dir / "stable_incremental_changes.json", changes)
    product_fields, product_values = _stable_product_rows(stable_current)
    _write_csv(output_dir / "stable_products.csv", product_fields, product_values)
    variant_fields, variant_values = variant_rows({"products": stable_current})
    _write_csv(output_dir / "stable_variants.csv", variant_fields, variant_values)
    excluded_fields, excluded_values = _exclusion_csv_rows(partition["excluded_products"])
    _write_csv(output_dir / "excluded_products.csv", excluded_fields, excluded_values)
    review_fields, review_values = _exclusion_csv_rows(partition["review_required"])
    _write_csv(output_dir / "review_required_stability.csv", review_fields, review_values)

    report_dir.mkdir(parents=True, exist_ok=True)
    _write_json(report_dir / "stable_pool_audit.json", audit)
    _write_csv(report_dir / "stable_pool_exclusions.csv", excluded_fields, excluded_values)
    _write_csv(report_dir / "review_required_stability.csv", review_fields, review_values)
    tracked_catalog_path = report_dir / "stable_catalog.json.gz"
    _write_gzip_json(tracked_catalog_path, master)
    audit["outputs"] = {
        "local_stable_catalog": str(stable_path),
        "tracked_stable_catalog_gzip": str(tracked_catalog_path),
        "tracked_stable_catalog_gzip_sha256": hashlib.sha256(tracked_catalog_path.read_bytes()).hexdigest(),
        "tracked_stable_catalog_gzip_size_bytes": tracked_catalog_path.stat().st_size,
        "local_source_snapshot": str(output_dir / "source_catalog.json"),
        "local_incremental_changes": str(output_dir / "stable_incremental_changes.json"),
    }
    _write_json(report_dir / "stable_pool_audit.json", audit)
    return audit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the read-only MIKIHOUSE stable regular-product catalog"
    )
    parser.add_argument("--special", type=Path, default=Path("special_skus_2026aw.csv"))
    parser.add_argument("--output", type=Path, default=Path("output/storefront-stable"))
    parser.add_argument(
        "--report-dir", type=Path, default=Path("deliverables/storefront_stable_catalog")
    )
    parser.add_argument(
        "--old-master", type=Path, default=Path("output/storefront-master/master_catalog.json")
    )
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--delay", type=float, default=0.1)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--max-pages", type=int, default=1000)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_stable_catalog_sync(
            args.special,
            args.output,
            args.report_dir,
            old_master_path=args.old_master,
            page_size=args.page_size,
            delay=args.delay,
            timeout=args.timeout,
            retries=args.retries,
            max_pages=args.max_pages,
        )
    except (OSError, ValueError, ScrapeError) as exc:
        raise SystemExit(f"stable catalog sync failed: {exc}") from exc
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0
