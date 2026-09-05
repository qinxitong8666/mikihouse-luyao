from __future__ import annotations

import csv
import gzip
import json
from pathlib import Path

from mikihouse_luyao.catalog import calculate_mini_program_price_jpy
from mikihouse_luyao.csv_input import read_product_numbers
from mikihouse_luyao.stable_catalog import (
    PERMANENT_NON_SELLABLE_PRODUCT_NUMBERS,
    STABLE,
    assess_product_stability,
)


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "deliverables/storefront_stable_catalog/stable_pool_audit.json"
CATALOG = ROOT / "deliverables/storefront_stable_catalog/stable_catalog.json.gz"


def test_checked_in_stable_catalog_matches_online_audit_and_has_zero_rule_leaks() -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    with gzip.open(CATALOG, "rt", encoding="utf-8") as stream:
        catalog = json.load(stream)
    special = set(read_product_numbers(ROOT / "special_skus_2026aw.csv"))
    products = catalog["products"]
    counts = report["counts"]
    assert report["status"] == "COMPLETED_READ_ONLY_STOREFRONT"
    assert catalog["schema_version"] == 3
    assert catalog["catalog_kind"] == "MIKIHOUSE_STABLE_REGULAR_PRODUCT_POOL"
    assert catalog["shijiu_action_source_required"] == "stable_catalog"
    assert len(products) == counts["stable_catalog_product_count"] == 2434
    assert sum(len(row["variants"]) for row in products) == counts["stable_catalog_variant_count"]
    assert sum(len(row["image_resources"]) for row in products) == counts["stable_catalog_image_resource_count"]
    assert not ({row["product_number"] for row in products} & special)
    assert not (
        {row["product_number"] for row in products}
        & set(PERMANENT_NON_SELLABLE_PRODUCT_NUMBERS)
    )
    assert report["explicit_signal_coverage"]["all_required_exclusions_passed"] is True
    assert report["explicit_signal_coverage"]["web_exclusive_signal_products_in_stable_catalog"] == []
    assert report["explicit_signal_coverage"]["limited_price_signal_products_in_stable_catalog"] == []
    for product in products:
        assert product["stability"]["status"] == STABLE
        assert assess_product_stability(product, special)["status"] == STABLE
        assert product["main_image"]["url"]
        assert product["ordered_images"]
        assert len(product["source_content_sha256"]) == 64
        assert len(product["shijiu_good_details"]) <= 1024
        assert "<img" not in product["shijiu_good_details"].lower()
        assert "http://" not in product["shijiu_good_details"].lower()
        assert "https://" not in product["shijiu_good_details"].lower()
        for variant in product["variants"]:
            assert variant["sku"]
            assert variant["tax_included_price_jpy"] > 0
            assert variant["resolved_image"]["url"]
            assert variant["mini_program_price_jpy"] == calculate_mini_program_price_jpy(
                variant["tax_included_price_jpy"]
            )


def test_audit_counts_partition_complete_storefront_and_record_zero_shijiu_writes() -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    counts = report["counts"]
    assert counts["storefront_total_product_count"] == (
        counts["pdf_special_list_online_excluded_count"]
        + counts["web_exclusive_excluded_count"]
        + counts["limited_time_price_excluded_count"]
        + counts["non_sellable_service_or_addon_excluded_count"]
        + counts["review_required_stability_count"]
        + counts["stable_catalog_product_count"]
    )
    assert counts["pdf_special_list_manifest_count"] == 351
    assert counts["pdf_special_list_offline_remembered_count"] == 40
    assert report["old_candidate_pool_comparison"]["actual_old_active_pool_count"] == 2615
    assert report["old_candidate_pool_comparison"]["stable_count_delta_vs_2615"] == -181
    assert report["old_candidate_pool_comparison"]["stable_count_delta_vs_2608"] == -174
    safety = report["shijiu_safety"]
    assert safety["operator_reported_concurrent_writer"] == "WAWU"
    assert safety["writer_mutex_evidence_generated"] is False
    assert all(
        safety[key] == 0
        for key in (
            "shijiu_read_requests",
            "shijiu_create_requests",
            "shijiu_update_requests",
            "shijiu_cos_upload_requests",
            "shijiu_shelf_or_inventory_or_price_writes",
        )
    )


def test_exclusion_csv_contains_all_online_exclusions_and_no_stable_rows() -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    with (ROOT / "deliverables/storefront_stable_catalog/stable_pool_exclusions.csv").open(
        encoding="utf-8-sig", newline=""
    ) as stream:
        rows = list(csv.DictReader(stream))
    expected = (
        report["counts"]["pdf_special_list_online_excluded_count"]
        + report["counts"]["web_exclusive_excluded_count"]
        + report["counts"]["limited_time_price_excluded_count"]
        + report["counts"]["non_sellable_service_or_addon_excluded_count"]
    )
    assert len(rows) == expected
    assert {row["excluded_reason"] for row in rows} == {
        "PDF_SPECIAL_LIST",
        "WEB_EXCLUSIVE",
        "LIMITED_TIME_PRICE",
        "NON_SELLABLE_SERVICE_OR_ADDON",
    }
