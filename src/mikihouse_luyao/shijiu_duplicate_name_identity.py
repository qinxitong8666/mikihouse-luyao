from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .shijiu_import import content_sha256, recursively_find_skus


UNIQUE_STRONG_MATCH = "UNIQUE_STRONG_MATCH"
NOT_FOUND = "NOT_FOUND"
AMBIGUOUS = "AMBIGUOUS"
TARGET_CATEGORY_ID = 294884


class DuplicateNameIdentityError(RuntimeError):
    pass


def _row_product_id(row: dict[str, Any]) -> str:
    return str(row.get("id") or row.get("good_id") or row.get("goods_id") or "").strip()


def _row_good_name(row: dict[str, Any]) -> str:
    return str(row.get("good_name") or row.get("goods_name") or row.get("name") or "").strip()


def _find_first(value: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        for key in keys:
            if key in value and value[key] not in (None, ""):
                return value[key]
        for child in value.values():
            found = _find_first(child, keys)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_first(child, keys)
            if found not in (None, ""):
                return found
    return None


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def _normalized_specification(value: Any) -> str:
    if isinstance(value, list):
        return ",".join(str(part).strip() for part in value if str(part).strip())
    return ",".join(part.strip() for part in str(value or "").split(",") if part.strip())


def expected_identity_from_payload(
    good_name: str,
    sku_info: list[dict[str, Any]],
    *,
    category_id: int = TARGET_CATEGORY_ID,
) -> dict[str, Any]:
    name = str(good_name or "").strip()
    if not name:
        raise DuplicateNameIdentityError("expected good_name must be non-empty")
    if not isinstance(sku_info, list) or not sku_info:
        raise DuplicateNameIdentityError("expected complete sku_info must be non-empty")
    by_code: dict[str, dict[str, Any]] = {}
    for row in sku_info:
        code = str(row.get("sku_code") or "").strip()
        if not code or not code.startswith("MIKI-"):
            raise DuplicateNameIdentityError("expected backend sku_code must use MIKI- prefix")
        if code in by_code:
            raise DuplicateNameIdentityError(f"duplicate expected backend sku_code: {code}")
        price = _decimal(row.get("sku_price"))
        if price is None:
            raise DuplicateNameIdentityError(f"invalid expected price: {code}")
        by_code[code] = {
            "sku_code": code,
            "sku_price": price,
            "spec_name": _normalized_specification(row.get("spec_name")),
        }
    ordered_codes = sorted(by_code)
    return {
        "good_name": name,
        "category_id": int(category_id),
        "backend_sku_codes": ordered_codes,
        "backend_sku_code_set_sha256": content_sha256(ordered_codes),
        "variant_count": len(ordered_codes),
        "by_code": by_code,
    }


def _candidate_observation(
    expected: dict[str, Any],
    row: dict[str, Any],
    detail: dict[str, Any],
) -> dict[str, Any]:
    product_id = _row_product_id(row)
    sku_rows = recursively_find_skus(detail)
    actual_codes = [str(sku.get("sku_code") or "").strip() for sku in sku_rows]
    nonempty_codes = [code for code in actual_codes if code]
    by_code = {str(sku.get("sku_code") or "").strip(): sku for sku in sku_rows if sku.get("sku_code")}
    exact_set = (
        len(nonempty_codes) == len(set(nonempty_codes))
        and set(nonempty_codes) == set(expected["backend_sku_codes"])
        and len(nonempty_codes) == expected["variant_count"]
    )
    actual_category = _find_first(detail, ("good_type",))
    category_match = str(actual_category) == str(expected["category_id"])
    name_match = _row_good_name(row) == expected["good_name"]
    price_matches: list[bool] = []
    spec_matches: list[bool] = []
    if exact_set:
        for code in expected["backend_sku_codes"]:
            actual = by_code[code]
            price_matches.append(
                _decimal(actual.get("sku_price", actual.get("price")))
                == expected["by_code"][code]["sku_price"]
            )
            spec_matches.append(
                _normalized_specification(
                    actual.get("spec_name") or actual.get("spec_son_name") or ""
                ) == expected["by_code"][code]["spec_name"]
            )
    prices_match = exact_set and all(price_matches)
    specifications_match = exact_set and all(spec_matches)
    strong_match = (
        bool(product_id)
        and name_match
        and exact_set
        and category_match
        and prices_match
        and specifications_match
    )
    return {
        "product_id": product_id,
        "good_name_exact": name_match,
        "actual_backend_sku_count": len(nonempty_codes),
        "actual_backend_sku_code_set_sha256": content_sha256(sorted(nonempty_codes)),
        "exact_complete_backend_sku_set": exact_set,
        "category_294884_match": category_match,
        "variant_count_match": len(nonempty_codes) == expected["variant_count"],
        "all_prices_match": prices_match,
        "all_specifications_match": specifications_match,
        "strong_match": strong_match,
    }


def resolve_duplicate_good_name_candidates(
    *,
    good_name: str,
    sku_info: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    detail_by_product_id: dict[str, dict[str, Any]],
    category_id: int = TARGET_CATEGORY_ID,
) -> dict[str, Any]:
    """Resolve exact-name candidates with the complete MIKI backend SKU set.

    good_name is only a search scope. A binding is possible only when exactly one
    candidate also matches the complete SKU set and every configured auxiliary
    identity condition. No list order, timestamps, fuzzy names, price similarity,
    or individual SKU overlap participates in the decision.
    """
    expected = expected_identity_from_payload(good_name, sku_info, category_id=category_id)
    unique_rows: dict[str, dict[str, Any]] = {}
    for row in candidate_rows:
        product_id = _row_product_id(row)
        if product_id and _row_good_name(row) == expected["good_name"]:
            unique_rows[product_id] = row
    missing_details = sorted(set(unique_rows) - set(detail_by_product_id))
    if missing_details:
        raise DuplicateNameIdentityError(
            "every exact-name candidate requires a getFormatInfo response"
        )
    observations = [
        _candidate_observation(expected, unique_rows[product_id], detail_by_product_id[product_id])
        for product_id in sorted(unique_rows)
    ]
    strong_ids = [row["product_id"] for row in observations if row["strong_match"]]
    if len(strong_ids) == 1:
        status = UNIQUE_STRONG_MATCH
        product_id: str | None = strong_ids[0]
    elif len(strong_ids) > 1:
        status = AMBIGUOUS
        product_id = None
    else:
        status = NOT_FOUND
        product_id = None
    return {
        "status": status,
        "binding_allowed": status == UNIQUE_STRONG_MATCH,
        "shijiu_product_id": product_id,
        "shijiu_sku_id": None,
        "good_name_role": "CANDIDATE_SCOPE_ONLY_NOT_BINDING_PROOF",
        "primary_identity": "EXACT_COMPLETE_BACKEND_SKU_CODE_SET",
        "required_auxiliary_conditions": [
            "CATEGORY_294884",
            "EXACT_VARIANT_COUNT",
            "EXACT_SPECIFICATION_STRUCTURE",
            "EXACT_VARIANT_PRICES",
        ],
        "expected_backend_sku_count": expected["variant_count"],
        "expected_backend_sku_code_set_sha256": expected["backend_sku_code_set_sha256"],
        "exact_name_candidate_count": len(unique_rows),
        "strong_match_count": len(strong_ids),
        "strong_match_product_ids": strong_ids,
        "candidate_observations": observations,
        "forbidden_identity_inputs": [
            "LIST_ORDER",
            "CREATED_AT",
            "FUZZY_NAME",
            "PRICE_SIMILARITY",
            "SINGLE_SKU_OVERLAP",
        ],
    }


def resolve_with_readonly_detail_loader(
    *,
    good_name: str,
    sku_info: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    detail_loader: Callable[[str], dict[str, Any]],
    category_id: int = TARGET_CATEGORY_ID,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    details = {
        product_id: detail_loader(product_id)
        for product_id in sorted({
            _row_product_id(row)
            for row in candidate_rows
            if _row_product_id(row) and _row_good_name(row) == str(good_name).strip()
        })
    }
    return resolve_duplicate_good_name_candidates(
        good_name=good_name,
        sku_info=sku_info,
        candidate_rows=candidate_rows,
        detail_by_product_id=details,
        category_id=category_id,
    ), details


def analyze_duplicate_names(stable_catalog: dict[str, Any]) -> dict[str, Any]:
    products = [row for row in stable_catalog.get("products") or [] if row.get("active", True)]
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    global_skus: Counter[str] = Counter()
    sku_sets: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for product in products:
        name = str(product.get("name") or "").strip()
        by_name[name].append(product)
        codes = tuple(sorted(
            f"MIKI-{str(variant.get('sku') or '').strip()}"
            for variant in product.get("variants") or []
            if variant.get("active", True) and str(variant.get("sku") or "").strip()
        ))
        sku_sets[codes].append(str(product.get("product_number") or ""))
        global_skus.update(codes)
    duplicate_groups = {name: rows for name, rows in by_name.items() if len(rows) > 1}
    group_size_distribution = Counter(len(rows) for rows in duplicate_groups.values())
    identical_sets = [
        {"product_numbers": numbers, "backend_sku_code_set_sha256": content_sha256(codes)}
        for codes, numbers in sku_sets.items()
        if codes and len(numbers) > 1
    ]
    duplicate_products = sum(len(rows) for rows in duplicate_groups.values())
    globally_duplicated_skus = sorted(code for code, count in global_skus.items() if count > 1)
    groups = []
    for name, rows in sorted(duplicate_groups.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        groups.append({
            "good_name": name,
            "product_count": len(rows),
            "product_numbers": [str(row["product_number"]) for row in rows],
            "products": [{
                "product_number": str(row["product_number"]),
                "variant_count": sum(
                    variant.get("active", True) for variant in row.get("variants") or []
                ),
                "backend_sku_code_set_sha256": content_sha256(sorted(
                    f"MIKI-{str(variant.get('sku') or '').strip()}"
                    for variant in row.get("variants") or []
                    if variant.get("active", True)
                )),
            } for row in rows],
        })
    return {
        "stable_catalog_product_count": len(products),
        "duplicate_name_group_count": len(duplicate_groups),
        "duplicate_name_product_count": duplicate_products,
        "group_size_distribution": {
            str(size): count for size, count in sorted(group_size_distribution.items())
        },
        "maximum_group_size": max(group_size_distribution, default=0),
        "maximum_groups": [
            {"good_name": name, "product_count": len(rows)}
            for name, rows in duplicate_groups.items()
            if len(rows) == max(group_size_distribution, default=0)
        ],
        "globally_duplicated_backend_sku_code_count": len(globally_duplicated_skus),
        "globally_duplicated_backend_sku_code_hashes": [
            hashlib.sha256(code.encode("utf-8")).hexdigest() for code in globally_duplicated_skus
        ],
        "identical_complete_backend_sku_set_group_count": len(identical_sets),
        "identical_complete_backend_sku_set_groups": identical_sets,
        "all_duplicate_name_products_have_source_unique_complete_sku_sets": (
            not globally_duplicated_skus and not identical_sets
        ),
        "theoretical_duplicate_name_review_release_count": (
            duplicate_products if not globally_duplicated_skus and not identical_sets else 0
        ),
        "groups": groups,
    }


def audit_price_outside_configured_range(
    stable_catalog: dict[str, Any],
    *,
    minimum_tax_included_price_jpy: int,
    maximum_tax_included_price_jpy: int,
) -> dict[str, Any]:
    previous_absolute_maximum = 1_000_000
    rows: list[dict[str, Any]] = []
    products_by_number = {
        str(product.get("product_number")): product
        for product in stable_catalog.get("products") or []
        if product.get("active", True)
    }
    for number, product in sorted(products_by_number.items()):
        active_variants = [
            variant for variant in product.get("variants") or [] if variant.get("active", True)
        ]
        product_prices = [int(variant["tax_included_price_jpy"]) for variant in active_variants]
        for variant in active_variants:
            price = int(variant["tax_included_price_jpy"])
            if minimum_tax_included_price_jpy <= price <= maximum_tax_included_price_jpy:
                continue
            if price <= 0:
                classification = "ZERO_PRICE_SOURCE_DATA"
                recommendation = "KEEP_INITIALIZATION_REVIEW_REQUIRED_DO_NOT_CREATE"
                interpretation = (
                    "The official source snapshot exposes zero JPY; no sellable target price "
                    "may be invented. Confirm whether this is a free add-on or non-product row."
                )
            else:
                classification = "PLAUSIBLE_REAL_HIGH_PRICE_REQUIRES_MANUAL_APPROVAL"
                recommendation = "KEEP_REVIEW_AND_EVALUATE_GUARD_IN_SEPARATE_BUSINESS_CHANGE"
                interpretation = (
                    "All observed variants are positive and the product is stable; the existing "
                    "maximum guard may be narrower than this genuine high-price product, but this "
                    "audit does not change the guard or authorize import."
                )
            rows.append({
                "product_number": number,
                "product_name": str(product.get("name") or ""),
                "variant_sku": str(variant.get("sku") or ""),
                "tax_included_price_jpy": price,
                "product_variant_count": len(active_variants),
                "product_price_min_jpy": min(product_prices) if product_prices else None,
                "product_price_max_jpy": max(product_prices) if product_prices else None,
                "all_product_variants_same_price": len(set(product_prices)) == 1,
                "compare_at_price_jpy": variant.get("compare_at_price_jpy"),
                "classification": classification,
                "recommendation": recommendation,
                "interpretation": interpretation,
            })
    classifications = Counter(row["classification"] for row in rows)
    source_ceiling_changed = (
        minimum_tax_included_price_jpy == 1
        and maximum_tax_included_price_jpy == 2_000_000
    )
    released_product_numbers = sorted({
        str(product.get("product_number") or "")
        for product in products_by_number.values()
        if source_ceiling_changed
        and any(
            previous_absolute_maximum < int(variant["tax_included_price_jpy"])
            <= maximum_tax_included_price_jpy
            for variant in product.get("variants") or []
            if variant.get("active", True)
        )
    })
    return {
        "configured_range": {
            "minimum_tax_included_price_jpy": minimum_tax_included_price_jpy,
            "maximum_tax_included_price_jpy": maximum_tax_included_price_jpy,
        },
        "outside_range_variant_count": len(rows),
        "outside_range_product_count": len({row["product_number"] for row in rows}),
        "classification_counts": dict(sorted(classifications.items())),
        "guard_changed": source_ceiling_changed,
        "guard_change_scope": "SOURCE_ABSOLUTE_ELIGIBILITY_ONLY" if source_ceiling_changed else "NONE",
        "previous_maximum_tax_included_price_jpy": (
            previous_absolute_maximum if source_ceiling_changed else maximum_tax_included_price_jpy
        ),
        "price_change_absolute_and_relative_guards_changed": False,
        "automatic_import_release_count": len(released_product_numbers),
        "released_product_numbers": released_product_numbers,
        "rows": rows,
    }
