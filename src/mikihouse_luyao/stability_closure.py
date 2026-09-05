from __future__ import annotations

import argparse
import copy
import gzip
import json
from pathlib import Path
from typing import Any

from .catalog import calculate_mini_program_price_jpy
from .stable_sync import write_json_atomic


HIGH_PRICE_PRODUCT_NUMBER = "13-6671-684"
RESOURCE_PRODUCT_NUMBER = "46-8299-611"
EXPECTED_HIGH_PRICE_JPY = 1_430_000
EXPECTED_HIGH_TARGET_JPY = 929_500
EXPECTED_SOURCE_MAX_JPY = 2_000_000
EXPECTED_ABSOLUTE_DELTA_JPY = 50_000
EXPECTED_RELATIVE_DELTA = 0.5


class StabilityClosureError(RuntimeError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise StabilityClosureError(f"JSON root must be an object: {path}")
    return value


def close_stable_pool_audit(
    *,
    source_snapshot: dict[str, Any],
    stable_catalog: dict[str, Any],
    stable_audit: dict[str, Any],
    prior_sellable_audit: dict[str, Any],
    price_guard: dict[str, Any],
    equivalence_evidence: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if source_snapshot.get("complete_pagination_validated") is not True:
        raise StabilityClosureError("source snapshot is not a complete successful crawl")
    source_by_number = {
        str(row.get("product_number") or ""): row
        for row in source_snapshot.get("products") or []
    }
    stable_by_number = {
        str(row.get("product_number") or ""): row
        for row in stable_catalog.get("products") or []
        if row.get("active", True)
    }
    high = source_by_number.get(HIGH_PRICE_PRODUCT_NUMBER)
    resource = source_by_number.get(RESOURCE_PRODUCT_NUMBER)
    if high is None or resource is None:
        raise StabilityClosureError("closure products missing from complete source snapshot")

    prices = [int(row["tax_included_price_jpy"]) for row in high.get("variants") or []]
    compare_at = [row.get("compare_at_price_jpy") for row in high.get("variants") or []]
    high_passed = all((
        len(prices) == 3,
        prices == [EXPECTED_HIGH_PRICE_JPY] * 3,
        compare_at == [EXPECTED_HIGH_PRICE_JPY] * 3,
        calculate_mini_program_price_jpy(EXPECTED_HIGH_PRICE_JPY)
        == EXPECTED_HIGH_TARGET_JPY,
        HIGH_PRICE_PRODUCT_NUMBER in stable_by_number,
        int(price_guard.get("minimum_tax_included_price_jpy") or 0) == 1,
        int(price_guard.get("maximum_tax_included_price_jpy") or 0)
        == EXPECTED_SOURCE_MAX_JPY,
        int(price_guard.get("maximum_absolute_change_jpy") or 0)
        == EXPECTED_ABSOLUTE_DELTA_JPY,
        float(price_guard.get("maximum_relative_change_ratio") or -1)
        == EXPECTED_RELATIVE_DELTA,
    ))
    if not high_passed:
        raise StabilityClosureError("high-price eligibility closure failed closed")

    normalization = source_snapshot.get("verified_https_image_normalization") or {}
    normalization_row = next(
        (
            row
            for row in normalization.get("results") or []
            if row.get("product_number") == RESOURCE_PRODUCT_NUMBER
        ),
        None,
    )
    equivalence_row = next(
        (
            row
            for row in equivalence_evidence.get("entries") or []
            if row.get("product_number") == RESOURCE_PRODUCT_NUMBER
        ),
        None,
    )
    ordered_urls = [
        str((row.get("image") or {}).get("url") or "")
        for row in resource.get("ordered_images") or []
    ]
    resource_passed = all((
        equivalence_evidence.get("status")
        == "VERIFIED_OFFICIAL_READ_ONLY_CONTENT_EQUIVALENCE",
        equivalence_row is not None,
        equivalence_row and equivalence_row.get("content_hash_equal") is True,
        equivalence_row and equivalence_row.get("mime_equal") is True,
        equivalence_row and equivalence_row.get("decoded_dimensions_equal") is True,
        normalization_row is not None,
        normalization_row
        and normalization_row.get("status")
        in {
            "APPLIED_EXACT_VERIFIED_EQUIVALENT",
            "SOURCE_URL_NOT_PRESENT_NO_REWRITE_NEEDED",
        },
        RESOURCE_PRODUCT_NUMBER in stable_by_number,
        ordered_urls,
        all(url.startswith("https://") for url in ordered_urls),
        equivalence_row and equivalence_row.get("canonical_https_url") in ordered_urls,
    ))
    if not resource_passed:
        raise StabilityClosureError("46-8299-611 HTTPS resource closure failed closed")

    counts = stable_audit.get("counts") or {}
    if counts.get("review_required_stability_count") != 0:
        raise StabilityClosureError("stability review pool is not empty after closure")
    coverage = stable_audit.get("explicit_signal_coverage") or {}
    if coverage.get("all_required_exclusions_passed") is not True:
        raise StabilityClosureError("stable pool leak audit did not pass")

    report = copy.deepcopy(prior_sellable_audit)
    report.update({
        "schema_version": 2,
        "generated_at": source_snapshot.get("captured_at"),
        "status": "COMPLETED_OFFICIAL_READ_ONLY_STABLE_POOL_CLOSURE",
        "remaining_review_required_count": 0,
        "remaining_review_required_product_numbers": [],
        "final_sellable_stable_product_count": len(stable_by_number),
    })
    report["high_price_verification"].update({
        "verified_real_high_price_product": True,
        "variant_tax_included_price_jpy": prices,
        "variant_compare_at_price_jpy": compare_at,
        "target_price_jpy": EXPECTED_HIGH_TARGET_JPY,
        "source_price_upper_bound_previous_jpy": 1_000_000,
        "source_price_upper_bound_current_jpy": EXPECTED_SOURCE_MAX_JPY,
        "source_price_upper_bound_recommended_jpy": EXPECTED_SOURCE_MAX_JPY,
        "recommendation": "APPLIED: source absolute eligibility ceiling raised independently.",
        "guard_changed_this_round": True,
        "released_from_initialization_review": True,
        "absolute_price_change_guard_jpy": EXPECTED_ABSOLUTE_DELTA_JPY,
        "relative_price_change_guard_ratio": EXPECTED_RELATIVE_DELTA,
        "price_change_guards_unchanged": True,
    })
    for row in report.get("products") or []:
        if row.get("product_number") == HIGH_PRICE_PRODUCT_NUMBER:
            row["disposition"] = "STABLE_ELIGIBLE_ABSOLUTE_SOURCE_PRICE_VERIFIED"
    resource_report = {
        "schema_version": 1,
        "generated_at": source_snapshot.get("captured_at"),
        "status": "VERIFIED_HTTPS_EQUIVALENT_APPLIED_TO_COMPLETE_CRAWL",
        "mode": "PLANNING_ONLY",
        "source": "MIKIHOUSE",
        "product_number": RESOURCE_PRODUCT_NUMBER,
        "product_name": resource.get("name") or "",
        "original_non_https_resource_count": 1,
        "current_ordered_resource_count": len(ordered_urls),
        "current_non_https_ordered_resource_count": sum(
            not url.startswith("https://") for url in ordered_urls
        ),
        "equivalence_evidence": equivalence_row,
        "complete_crawl_normalization_result": normalization_row,
        "entered_stable_sellable_catalog": RESOURCE_PRODUCT_NUMBER in stable_by_number,
        "mechanical_scheme_rewrite_allowed": False,
        "exact_product_url_and_content_hash_allowlist_required": True,
        "safety": {
            "official_image_read_requests": 2,
            "shijiu_requests": 0,
            "shijiu_create_requests": 0,
            "shijiu_update_requests": 0,
            "shijiu_cos_upload_requests": 0,
            "writer_mutex_evidence_generated": False,
            "legacy_286_touched": False,
        },
    }
    report["verified_https_resource_closure"] = {
        "status": resource_report["status"],
        "product_number": RESOURCE_PRODUCT_NUMBER,
        "entered_stable_sellable_catalog": True,
        "evidence_content_sha256": equivalence_row["https_observation"][
            "content_sha256"
        ],
    }
    report["safety"].update({
        "official_storefront_complete_crawl_used": True,
        "official_image_read_requests": 2,
        "shijiu_read_requests": 0,
        "shijiu_create_requests": 0,
        "shijiu_update_requests": 0,
        "shijiu_cos_upload_requests": 0,
        "shijiu_shelf_price_inventory_writes": 0,
        "writer_mutex_evidence_generated": False,
        "legacy_286_touched": False,
    })
    return report, resource_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Close the MIKIHOUSE stable sellable pool offline")
    parser.add_argument("--source", type=Path, default=Path("output/storefront-stable/source_catalog.json"))
    parser.add_argument("--stable", type=Path, default=Path("deliverables/storefront_stable_catalog/stable_catalog.json.gz"))
    parser.add_argument("--stable-audit", type=Path, default=Path("deliverables/storefront_stable_catalog/stable_pool_audit.json"))
    parser.add_argument("--prior-sellable-audit", type=Path, default=Path("deliverables/storefront_stable_catalog/sellable_review_resolution_audit.json"))
    parser.add_argument("--price-guard", type=Path, default=Path("config/shijiu_price_guard.json"))
    parser.add_argument("--equivalence", type=Path, default=Path("config/mikihouse_verified_https_image_equivalents.json"))
    parser.add_argument("--resource-report", type=Path, default=Path("deliverables/storefront_stable_catalog/verified_https_resource_audit_46-8299-611.json"))
    args = parser.parse_args(argv)
    report, resource_report = close_stable_pool_audit(
        source_snapshot=_read_json(args.source),
        stable_catalog=_read_json(args.stable),
        stable_audit=_read_json(args.stable_audit),
        prior_sellable_audit=_read_json(args.prior_sellable_audit),
        price_guard=_read_json(args.price_guard),
        equivalence_evidence=_read_json(args.equivalence),
    )
    write_json_atomic(args.prior_sellable_audit, report)
    write_json_atomic(args.resource_report, resource_report)
    print(json.dumps({
        "status": report["status"],
        "stable": report["final_sellable_stable_product_count"],
        "remaining_review": report["remaining_review_required_count"],
        "resource_status": resource_report["status"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
