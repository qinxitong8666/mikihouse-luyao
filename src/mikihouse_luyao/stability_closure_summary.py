from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
from typing import Any

from .shijiu_import import content_sha256
from .stable_sync import write_json_atomic


class StabilityClosureSummaryError(RuntimeError):
    pass


def _read(path: Path) -> dict[str, Any]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise StabilityClosureSummaryError(f"JSON root must be an object: {path}")
    return value


def build_summary(
    stable: dict[str, Any],
    source: dict[str, Any],
    audit: dict[str, Any],
    initialization: dict[str, Any],
    pilot: dict[str, Any],
    resource_audit: dict[str, Any],
    price_guard: dict[str, Any],
) -> dict[str, Any]:
    counts = audit.get("counts") or {}
    init_counts = initialization.get("counts") or initialization.get("initialization_counts") or {}
    leak = initialization.get("initialization_plan_leak_audit") or {}
    pilot_fresh = all((
        pilot.get("product_count") == 20,
        pilot.get("status") == "FROZEN_PLANNING_ONLY",
        pilot.get("execution_authorized") is False,
        (pilot.get("freshness_guard") or {}).get("stable_catalog_logical_sha256")
        == content_sha256(stable),
        (pilot.get("freshness_guard") or {}).get("source_snapshot_logical_sha256")
        == content_sha256(source),
    ))
    required = all((
        source.get("complete_pagination_validated") is True,
        counts.get("stable_catalog_product_count") == 2435,
        counts.get("review_required_stability_count") == 0,
        init_counts.get("initialization_review_required_count") == 0,
        init_counts.get("planned_initial_create_product_count") == 2387,
        leak.get("passed") is True,
        leak.get("all_forbidden_leak_product_numbers") == [],
        resource_audit.get("status")
        == "VERIFIED_HTTPS_EQUIVALENT_APPLIED_TO_COMPLETE_CRAWL",
        price_guard.get("minimum_tax_included_price_jpy") == 1,
        price_guard.get("maximum_tax_included_price_jpy") == 2_000_000,
        price_guard.get("maximum_absolute_change_jpy") == 50_000,
        price_guard.get("maximum_relative_change_ratio") == 0.5,
        pilot_fresh,
    ))
    if not required:
        raise StabilityClosureSummaryError("final stable-pool closure evidence is incomplete")
    return {
        "schema_version": 1,
        "generated_at": source.get("captured_at"),
        "status": "COMPLETED_PLANNING_ONLY_STABLE_POOL_CLOSURE",
        "mode": "PLANNING_ONLY",
        "write_status": "SHIJIU_WRITE_BLOCKED_CONCURRENT_WRITER",
        "source": "MIKIHOUSE",
        "target": "SHIJIU",
        "counts": {
            "storefront_total_product_count": counts["storefront_total_product_count"],
            "final_sellable_stable_product_count": counts["stable_catalog_product_count"],
            "stable_variant_count": counts["stable_catalog_variant_count"],
            "stable_image_resource_count": counts["stable_catalog_image_resource_count"],
            "pdf_special_list_manifest_count": counts["pdf_special_list_manifest_count"],
            "pdf_special_list_online_excluded_count": counts[
                "pdf_special_list_online_excluded_count"
            ],
            "pdf_special_list_offline_remembered_count": counts[
                "pdf_special_list_offline_remembered_count"
            ],
            "web_exclusive_excluded_count": counts["web_exclusive_excluded_count"],
            "limited_time_price_excluded_count": counts[
                "limited_time_price_excluded_count"
            ],
            "non_sellable_service_or_addon_excluded_count": counts[
                "non_sellable_service_or_addon_excluded_count"
            ],
            "review_required_stability_count": counts[
                "review_required_stability_count"
            ],
            "initialization_review_required_count": init_counts[
                "initialization_review_required_count"
            ],
            "planned_initial_create_product_count": init_counts[
                "planned_initial_create_product_count"
            ],
            "mapped_handoff_count": init_counts["already_mapped_handoff_count"],
            "historical_frozen_count": init_counts["historical_frozen_count"],
            "initialization_batch_count": init_counts["batch_count"],
        },
        "price_policy": {
            "absolute_source_minimum_jpy": 1,
            "absolute_source_maximum_jpy": 2_000_000,
            "absolute_change_review_threshold_jpy": 50_000,
            "relative_change_review_threshold_ratio": 0.5,
            "absolute_source_eligibility_and_change_guards_are_independent": True,
            "verified_1430000_source_price_target_jpy": 929_500,
        },
        "resource_closure": {
            "product_number": "46-8299-611",
            "status": resource_audit["status"],
            "content_hash_equal": resource_audit["equivalence_evidence"][
                "content_hash_equal"
            ],
            "current_non_https_ordered_resource_count": resource_audit[
                "current_non_https_ordered_resource_count"
            ],
            "mechanical_scheme_rewrite_allowed": False,
        },
        "pilot_20": {
            "product_count": pilot["product_count"],
            "coverage": pilot["coverage"],
            "fresh_and_valid": pilot_fresh,
            "execution_authorized": False,
            "execution_count": 0,
        },
        "initialization_plan_leak_audit": leak,
        "safety": {
            "shijiu_requests": 0,
            "shijiu_create_requests": 0,
            "shijiu_update_requests": 0,
            "shijiu_cos_upload_requests": 0,
            "shijiu_shelf_price_inventory_writes": 0,
            "writer_mutex_evidence_generated": False,
            "pilot_executed": False,
            "legacy_286_touched": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize final MIKIHOUSE stable-pool closure")
    parser.add_argument("--stable", type=Path, default=Path("deliverables/storefront_stable_catalog/stable_catalog.json.gz"))
    parser.add_argument("--source", type=Path, default=Path("output/storefront-stable/source_catalog.json"))
    parser.add_argument("--audit", type=Path, default=Path("deliverables/storefront_stable_catalog/stable_pool_audit.json"))
    parser.add_argument("--initialization", type=Path, default=Path("deliverables/shijiu_initialization/sellable_initialization_resolution_report.json"))
    parser.add_argument("--pilot", type=Path, default=Path("deliverables/shijiu_initialization/stable_pilot_20_frozen_plan.json"))
    parser.add_argument("--resource-audit", type=Path, default=Path("deliverables/storefront_stable_catalog/verified_https_resource_audit_46-8299-611.json"))
    parser.add_argument("--price-guard", type=Path, default=Path("config/shijiu_price_guard.json"))
    parser.add_argument("--output", type=Path, default=Path("deliverables/storefront_stable_catalog/stable_pool_final_closure_report.json"))
    args = parser.parse_args(argv)
    summary = build_summary(
        _read(args.stable), _read(args.source), _read(args.audit),
        _read(args.initialization), _read(args.pilot), _read(args.resource_audit),
        _read(args.price_guard),
    )
    write_json_atomic(args.output, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
