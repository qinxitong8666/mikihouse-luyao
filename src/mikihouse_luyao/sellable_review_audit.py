from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .catalog import calculate_mini_program_price_jpy
from .stable_catalog import (
    NON_SELLABLE_SERVICE_OR_ADDON,
    PERMANENT_NON_SELLABLE_PRODUCT_NUMBERS,
    REVIEW_REQUIRED,
    STABLE,
    _non_sellable_service_or_addon_evidence,
    assess_product_stability,
    partition_stable_catalog,
)
from .scraper import USER_AGENT
from .stable_sync import validate_complete_snapshot, write_json_atomic


HIGH_PRICE_PRODUCT_NUMBER = "13-6671-684"
HIGH_PRICE_TAX_INCLUDED_JPY = 1_430_000
HIGH_PRICE_TARGET_JPY = 929_500
AUDIT_SCHEMA_VERSION = 1


class SellableReviewAuditError(RuntimeError):
    pass


def _page_section(raw_html: str, product_number: str) -> str:
    marker = f"商品番号: {product_number}"
    start = raw_html.find(marker)
    if start < 0:
        return ""
    end = raw_html.find("</section>", start)
    return raw_html[start : end if end >= 0 else start + 250_000]


def observe_official_product_page(
    product: dict[str, Any],
    raw: bytes,
    *,
    final_url: str,
    http_status: int,
) -> dict[str, Any]:
    text = raw.decode("utf-8", errors="replace")
    section = _page_section(text, str(product["product_number"]))
    button_tags = re.findall(r"<button\b[^>]*\bname=[\"']add[\"'][^>]*>", section, re.I)
    expected_prices = sorted({
        int(row["tax_included_price_jpy"]) for row in product.get("variants") or []
    })
    expected_skus = sorted(str(row.get("sku") or "") for row in product.get("variants") or [])
    page_price_tokens = [f"¥{value:,}" for value in expected_prices]
    product_type = str(product.get("product_type") or "")
    # Shopify renders generic ``price__sale`` CSS classes even when the
    # compare-at price equals the current price.  Only visible text is valid
    # page evidence; markup attributes and scripts must not create a false
    # promotion signal.
    visible_section = re.sub(
        r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>",
        " ",
        section,
        flags=re.I | re.S,
    )
    visible_section = html.unescape(re.sub(r"<[^>]+>", " ", visible_section))
    promo_markers = sorted(set(re.findall(
        r"期間\s*限定\s*価格|特別\s*価格|\bSALE\b|セール",
        visible_section,
        flags=re.I,
    )))
    canonical_identity_marker = (
        f'href="https://www.mikihouse.co.jp/products/{product["product_number"]}"' in text
        or f'"handle":"{product["product_number"]}"' in text
        or f'data-url="/products/{product["product_number"]}"' in text
    )
    return {
        "http_status": http_status,
        "final_url": final_url,
        "final_url_matches_product": final_url.rstrip("/").endswith(
            f"/products/{product['product_number']}"
        ),
        "response_utf8_byte_count": len(raw),
        "response_sha256": hashlib.sha256(raw).hexdigest(),
        "exact_product_number_marker_present": bool(section) or canonical_identity_marker,
        "normal_product_detail_section_present": bool(section),
        "exact_product_name_present": html.escape(str(product.get("name") or "")) in text
        or str(product.get("name") or "") in html.unescape(text),
        "official_product_type_present": product_type in text,
        "all_exact_variant_skus_present": all(sku and sku in text for sku in expected_skus),
        "cart_add_form_present": bool(re.search(
            r"<form\b[^>]*\baction=[\"']/cart/add[\"']", section, re.I
        )),
        "add_button_count": len(button_tags),
        "any_enabled_add_button": any(
            not re.search(r"\bdisabled(?:\s*=|\s|>)", tag, re.I) for tag in button_tags
        ),
        "all_add_buttons_disabled": bool(button_tags) and all(
            re.search(r"\bdisabled(?:\s*=|\s|>)", tag, re.I) for tag in button_tags
        ),
        "expected_display_price_tokens": page_price_tokens,
        "all_expected_display_price_tokens_present": all(
            token in section for token in page_price_tokens
        ),
        "product_section_promotional_text_markers": promo_markers,
        "raw_html_committed": False,
    }


def fetch_official_product_page(
    url: str,
    *,
    timeout: float = 30,
    retries: int = 2,
) -> tuple[bytes, str, int]:
    last_error: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            request = Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept-Language": "ja-JP,ja;q=0.9"},
            )
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed official URLs
                return response.read(), response.geturl(), int(response.status)
        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(0.5 * (2**attempt))
    raise SellableReviewAuditError(f"official page read failed: {url}: {last_error}")


def _initial_review_numbers(quality_audit: dict[str, Any]) -> list[str]:
    return sorted({
        str(row.get("product_number") or "")
        for row in quality_audit.get("review_required_products") or []
        if row.get("product_number")
    })


def _review_numbers_for_issue_codes(
    quality_audit: dict[str, Any], issue_codes: set[str]
) -> list[str]:
    return sorted({
        str(row["product_number"])
        for row in quality_audit.get("review_required_products") or []
        if any(
            str(issue.get("code") or "") in issue_codes
            for issue in row.get("issues") or []
        )
    })


def _high_price_verification(product: dict[str, Any]) -> dict[str, Any]:
    variants = product.get("variants") or []
    prices = [int(row["tax_included_price_jpy"]) for row in variants]
    compare_at = [row.get("compare_at_price_jpy") for row in variants]
    names_and_identity_stable = (
        product.get("product_number") == HIGH_PRICE_PRODUCT_NUMBER
        and product.get("name") == "【ゴールドレーベル】ビキューナ×ベビーカシミヤ セーター（大人用）"
        and len(variants) == 3
        and all(str(row.get("sku") or "").startswith(f"{HIGH_PRICE_PRODUCT_NUMBER}00") for row in variants)
    )
    price_stable = prices == [HIGH_PRICE_TAX_INCLUDED_JPY] * 3
    no_structured_promotion = all(
        value in (None, HIGH_PRICE_TAX_INCLUDED_JPY) for value in compare_at
    )
    fields = "\n".join([
        str(product.get("name") or ""),
        str(product.get("product_type") or ""),
        "\n".join(str(value) for value in product.get("tags") or []),
        str(product.get("description") or ""),
    ])
    no_explicit_promotion = not re.search(
        r"期間\s*限定\s*価格|特別\s*価格|SALE|セール", fields, flags=re.I
    )
    target = calculate_mini_program_price_jpy(HIGH_PRICE_TAX_INCLUDED_JPY)
    verified = all((
        names_and_identity_stable,
        price_stable,
        no_structured_promotion,
        no_explicit_promotion,
        target == HIGH_PRICE_TARGET_JPY,
    ))
    return {
        "verified_real_high_price_product": verified,
        "variant_count": len(variants),
        "variant_skus": [str(row.get("sku") or "") for row in variants],
        "variant_tax_included_price_jpy": prices,
        "variant_compare_at_price_jpy": compare_at,
        "all_three_variants_exactly_1430000_jpy": price_stable,
        "structured_compare_at_promotion_present": not no_structured_promotion,
        "explicit_sale_or_limited_price_signal_present": not no_explicit_promotion,
        "target_price_jpy": target,
        "target_price_formula": "ceil(tax_included_price_jpy * 0.65)",
        "source_price_upper_bound_current_jpy": 1_000_000,
        "source_price_upper_bound_recommended_jpy": 2_000_000,
        "recommendation": (
            "Separate the absolute valid source-price ceiling from price-change anomaly guards; "
            "a future separately authorized change may raise only the source ceiling to JPY 2,000,000."
        ),
        "guard_changed_this_round": False,
    }


def build_sellable_review_resolution_audit(
    *,
    source_snapshot: dict[str, Any],
    previous_quality_audit: dict[str, Any],
    special_product_numbers: set[str],
    page_loader: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    products = validate_complete_snapshot(source_snapshot)
    by_number = {str(row["product_number"]): row for row in products}
    initial_review = _initial_review_numbers(previous_quality_audit)
    if len(initial_review) != 27:
        raise SellableReviewAuditError(
            f"expected exactly 27 prior initialization reviews, got {len(initial_review)}"
        )
    targets = sorted(set(initial_review) | set(PERMANENT_NON_SELLABLE_PRODUCT_NUMBERS))
    missing = sorted(set(targets) - set(by_number))
    if missing:
        raise SellableReviewAuditError(f"audit targets missing from complete crawl: {missing}")

    rows = []
    for number in targets:
        product = by_number[number]
        observation = page_loader(product)
        if (
            observation.get("http_status") != 200
            or not observation.get("final_url_matches_product")
            or not observation.get("exact_product_number_marker_present")
            or not observation.get("all_exact_variant_skus_present")
        ):
            raise SellableReviewAuditError(f"official page identity evidence incomplete: {number}")
        decision = assess_product_stability(product, special_product_numbers)
        non_sellable = _non_sellable_service_or_addon_evidence(product)
        if non_sellable is not None:
            disposition = "EXCLUDED_NON_SELLABLE_SERVICE_OR_ADDON"
        elif number == HIGH_PRICE_PRODUCT_NUMBER:
            disposition = "REVIEW_REQUIRED_REAL_HIGH_PRICE_GUARD_UNCHANGED"
        else:
            disposition = "REVIEW_REQUIRED_UNRESOLVED"
        rows.append({
            "product_number": number,
            "name": product.get("name") or "",
            "was_in_prior_27_initialization_reviews": number in initial_review,
            "official_storefront_evidence": {
                "product_type": product.get("product_type") or "",
                "tags": product.get("tags") or [],
                "description_character_count": len(str(product.get("description") or "")),
                "variant_count": len(product.get("variants") or []),
                "variant_skus": [str(row.get("sku") or "") for row in product.get("variants") or []],
                "variant_prices_jpy": [
                    int(row["tax_included_price_jpy"]) for row in product.get("variants") or []
                ],
                "variant_compare_at_prices_jpy": [
                    row.get("compare_at_price_jpy") for row in product.get("variants") or []
                ],
                "variant_available_for_sale": [
                    bool(row.get("available_for_sale")) for row in product.get("variants") or []
                ],
                "main_image_present": bool((product.get("main_image") or {}).get("url")),
                "ordered_image_count": len(product.get("ordered_images") or []),
                "resolved_variant_image_count": sum(
                    bool((row.get("resolved_image") or {}).get("url"))
                    for row in product.get("variants") or []
                ),
                "product_url": product.get("product_url") or "",
            },
            "official_page_evidence": observation,
            "classification_evidence": non_sellable,
            "stable_catalog_decision": decision,
            "disposition": disposition,
        })

    partition = partition_stable_catalog(products, special_product_numbers, source_snapshot["captured_at"])
    non_sellable_rows = [
        row for row in rows if row["disposition"] == "EXCLUDED_NON_SELLABLE_SERVICE_OR_ADDON"
    ]
    unresolved = [row for row in rows if row["disposition"].startswith("REVIEW_REQUIRED")]
    prior_missing_media = _review_numbers_for_issue_codes(
        previous_quality_audit,
        {"MISSING_MAIN_IMAGE", "MISSING_ORDERED_IMAGE", "MISSING_VARIANT_IMAGE"},
    )
    prior_zero_price = sorted(
        number
        for number in initial_review
        if any(
            int(variant.get("tax_included_price_jpy") or 0) <= 0
            for variant in by_number[number].get("variants") or []
        )
    )
    non_sellable_numbers = {
        row["product_number"] for row in non_sellable_rows
    }
    high_price = _high_price_verification(by_number[HIGH_PRICE_PRODUCT_NUMBER])
    high_price_page = next(
        row["official_page_evidence"]
        for row in rows
        if row["product_number"] == HIGH_PRICE_PRODUCT_NUMBER
    )
    high_price["official_page_has_normal_purchase_surface"] = all((
        high_price_page["normal_product_detail_section_present"],
        high_price_page["cart_add_form_present"],
        high_price_page["any_enabled_add_button"],
    ))
    high_price["official_page_visible_promotion_markers"] = high_price_page[
        "product_section_promotional_text_markers"
    ]
    high_price["official_page_visible_promotion_absent"] = not high_price[
        "official_page_visible_promotion_markers"
    ]
    high_price["verified_real_high_price_product"] = all((
        high_price["verified_real_high_price_product"],
        high_price["official_page_has_normal_purchase_surface"],
        high_price["official_page_visible_promotion_absent"],
    ))
    if not high_price["verified_real_high_price_product"]:
        raise SellableReviewAuditError("high-price product verification failed closed")
    stable_numbers = {row["product_number"] for row in partition["stable_products"]}
    leaked_non_sellable = sorted(
        stable_numbers & set(PERMANENT_NON_SELLABLE_PRODUCT_NUMBERS)
    )
    if leaked_non_sellable:
        raise SellableReviewAuditError(f"non-sellable products leaked: {leaked_non_sellable}")
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "generated_at": source_snapshot["captured_at"],
        "status": "COMPLETED_OFFICIAL_READ_ONLY_REVIEW_RESOLUTION",
        "mode": "PLANNING_ONLY",
        "source": "MIKIHOUSE",
        "initial_review_product_count": len(initial_review),
        "official_page_audit_product_count": len(rows),
        "official_page_audit_includes_previously_hidden_wrapping_addon": True,
        "non_sellable_service_or_addon_count": len(non_sellable_rows),
        "non_sellable_service_or_addon_product_numbers": [
            row["product_number"] for row in non_sellable_rows
        ],
        "remaining_review_required_count": len(unresolved),
        "remaining_review_required_product_numbers": [
            row["product_number"] for row in unresolved
        ],
        "prior_27_review_resolution": {
            "zero_price_source_data_product_count": len(prior_zero_price),
            "zero_price_source_data_product_numbers": prior_zero_price,
            "zero_price_products_confirmed_non_sellable_count": len(
                set(prior_zero_price) & non_sellable_numbers
            ),
            "zero_price_independent_sellable_products_remaining_review_count": len(
                set(prior_zero_price) - non_sellable_numbers
            ),
            "missing_media_product_count": len(prior_missing_media),
            "missing_media_product_numbers": prior_missing_media,
            "missing_media_overlap_zero_price_product_numbers": sorted(
                set(prior_missing_media) & set(prior_zero_price)
            ),
            "missing_media_confirmed_non_sellable_count": len(
                set(prior_missing_media) & non_sellable_numbers
            ),
            "sellable_missing_media_remaining_review_count": len(
                set(prior_missing_media) - non_sellable_numbers
            ),
            "high_price_review_product_numbers": [HIGH_PRICE_PRODUCT_NUMBER],
        },
        "high_price_verification": high_price,
        "final_sellable_stable_product_count": len(partition["stable_products"]),
        "stable_non_sellable_leak_product_numbers": leaked_non_sellable,
        "products": rows,
        "classification_policy": {
            "zero_price_alone_is_never_non_sellable_proof": True,
            "official_product_type_and_tags_are_primary_evidence": True,
            "official_product_page_identity_and_cart_structure_checked": True,
            "normal_sellable_requires_positive_price_valid_sku_variant_and_required_media": True,
            "reservation_or_made_to_order_not_newly_excluded": True,
        },
        "safety": {
            "official_storefront_complete_crawl_used": True,
            "official_product_page_get_requests": len(rows),
            "shijiu_read_requests": 0,
            "shijiu_create_requests": 0,
            "shijiu_update_requests": 0,
            "shijiu_cos_upload_requests": 0,
            "shijiu_shelf_price_inventory_writes": 0,
            "writer_mutex_evidence_generated": False,
            "legacy_286_touched": False,
        },
    }


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SellableReviewAuditError(f"JSON root must be an object: {path}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit the remaining stable-catalog sellability reviews using official read-only pages"
    )
    parser.add_argument(
        "--source-snapshot", type=Path, default=Path("output/storefront-stable/source_catalog.json")
    )
    parser.add_argument(
        "--previous-quality-audit",
        type=Path,
        default=Path("deliverables/shijiu_initialization/stable_initialization_data_quality_audit.json"),
    )
    parser.add_argument("--special", type=Path, default=Path("special_skus_2026aw.csv"))
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("deliverables/storefront_stable_catalog/sellable_review_resolution_audit.json"),
    )
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--delay", type=float, default=0.1)
    args = parser.parse_args(argv)

    from .csv_input import read_product_numbers

    source = _read_json(args.source_snapshot)
    previous_quality = _read_json(args.previous_quality_audit)
    special = set(read_product_numbers(args.special))

    def load_page(product: dict[str, Any]) -> dict[str, Any]:
        raw, final_url, status = fetch_official_product_page(
            str(product["product_url"]), timeout=args.timeout, retries=args.retries
        )
        if args.delay:
            time.sleep(args.delay)
        return observe_official_product_page(
            product, raw, final_url=final_url, http_status=status
        )

    report = build_sellable_review_resolution_audit(
        source_snapshot=source,
        previous_quality_audit=previous_quality,
        special_product_numbers=special,
        page_loader=load_page,
    )
    write_json_atomic(args.report, report)
    print(json.dumps({
        "status": report["status"],
        "audited": report["official_page_audit_product_count"],
        "non_sellable": report["non_sellable_service_or_addon_count"],
        "remaining_review": report["remaining_review_required_count"],
        "stable": report["final_sellable_stable_product_count"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
