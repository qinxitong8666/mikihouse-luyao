from __future__ import annotations

import copy
import gzip
import hashlib
import json
from pathlib import Path

from mikihouse_luyao.initialization_plan import (
    SOURCE,
    TARGET_CATEGORY_ID,
    WRITE_BLOCKED_STATUS,
    _source_product_fingerprint,
    freeze_initialization_batch,
    handoff_initialized_product,
    initialize_checkpoint,
    validate_historical_plan_is_permanently_stale,
    validate_plan_freshness,
)
from mikihouse_luyao.shijiu_import import content_sha256


ROOT = Path(__file__).resolve().parents[1]


def load(path: str) -> dict:
    target = ROOT / path
    opener = gzip.open if target.suffix == ".gz" else open
    with opener(target, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def test_historical_pilot_is_permanently_stale() -> None:
    stale = validate_historical_plan_is_permanently_stale(ROOT)
    assert stale["status"] == "STALE_BUSINESS_RULE_CHANGED"
    assert stale["must_never_execute"] is True
    assert len(stale["product_numbers"]) == 20


def test_new_pilot_is_stable_only_unmapped_and_frozen() -> None:
    pilot = load("deliverables/shijiu_initialization/stable_pilot_20_frozen_plan.json")
    stable = load("deliverables/storefront_stable_catalog/stable_catalog.json.gz")
    mapping = load("state/shijiu_mappings.json")
    stable_numbers = {row["product_number"] for row in stable["products"]}
    excluded = (
        set(stable["special_exclusion"]["online_product_numbers"])
        | set(stable["special_exclusion"]["offline_product_numbers"])
        | set(stable["stability_exclusion"]["web_exclusive_product_numbers"])
        | set(stable["stability_exclusion"]["limited_time_price_product_numbers"])
        | set(stable["stability_exclusion"]["non_sellable_service_or_addon_product_numbers"])
        | set(stable["stability_exclusion"]["review_required_product_numbers"])
    )
    numbers = [row["product_number"] for row in pilot["products"]]
    bound = {
        number
        for number, row in mapping["products"].items()
        if row.get("shijiu_product_id") not in (None, "")
    }
    assert pilot["status"] == "FROZEN_PLANNING_ONLY"
    assert pilot["write_status"] == WRITE_BLOCKED_STATUS
    assert pilot["execution_authorized"] is False
    assert pilot["safety"]["shijiu_requests"] == 0
    assert pilot["safety"]["writer_mutex_evidence_generated"] is False
    assert len(numbers) == len(set(numbers)) == 20
    assert set(numbers) <= stable_numbers
    assert not set(numbers) & excluded
    assert not set(numbers) & bound
    assert pilot["coverage"] == {"apparel": 5, "baby": 5, "footwear": 5, "goods": 5}
    assert pilot["freshness_guard"]["stable_catalog_logical_sha256"] == content_sha256(stable)
    assert all(
        row["create_readback_identity_contract"]["accepted_outcome"]
        == "UNIQUE_STRONG_MATCH_ONLY"
        for row in pilot["products"]
    )


def test_full_plan_accounts_for_all_stable_products_and_has_no_target_requests() -> None:
    plan = load("deliverables/shijiu_initialization/stable_initialization_batch_plan.json.gz")
    counts = plan["counts"]
    assert plan["status"] == "PLANNING_ONLY"
    assert plan["write_status"] == WRITE_BLOCKED_STATUS
    assert counts["stable_catalog_product_count"] == 2435
    assert counts["accounted_product_count"] == 2435
    assert counts["planned_initial_create_product_count"] == 2387
    assert counts["already_mapped_handoff_count"] == 6
    assert counts["historical_frozen_count"] == 42
    assert counts["initialization_review_required_count"] == 0
    assert counts["batch_count"] == 170
    assert (
        counts["planned_initial_create_product_count"]
        + counts["already_mapped_handoff_count"]
        + counts["historical_frozen_count"]
        + counts["initialization_review_required_count"]
        == 2435
    )
    assert sum(row["product_count"] for row in plan["batches"]) == len(plan["products"])
    assert plan["safety"]["shijiu_requests"] == 0
    assert plan["safety"]["shijiu_create_requests"] == 0
    assert plan["safety"]["shijiu_update_requests"] == 0
    assert plan["safety"]["shijiu_cos_upload_requests"] == 0
    assert plan["safety"]["official_image_download_count"] == 0


def test_every_planned_product_has_bounded_stages_and_variant_contract() -> None:
    plan = load("deliverables/shijiu_initialization/stable_initialization_batch_plan.json.gz")
    for product in plan["products"]:
        assert product["target_category_id"] == TARGET_CATEGORY_ID
        assert product["shijiu_product_id"] is None
        assert product["stages"][0]["key"] == "CREATE_CORE"
        assert product["stages"][0]["operation"] == "CREATE"
        assert product["stages"][0]["broadcast_count"] <= 4
        prior_broadcast = 0
        prior_details = 0
        for stage in product["stages"]:
            assert stage["mutation_retry_count"] == 0
            assert stage["payload_contains_credentials"] is False
            assert stage["payload_contains_target_cos_urls"] is False
            assert stage["broadcast_count"] - prior_broadcast <= 8
            assert stage["good_detail_pics_count"] - prior_details <= 8
            prior_broadcast = stage["broadcast_count"]
            prior_details = stage["good_detail_pics_count"]
        assert prior_broadcast == product["broadcast_count"]
        assert prior_details == product["good_detail_pics_count"]
        for variant in product["variants"]:
            assert variant["shijiu_sku_id"] is None
            assert variant["source_variant_id"].startswith(f"{SOURCE}:")
            assert variant["target_stock"] == (1 if variant["available_for_sale"] else 0)
            assert variant["target_price_jpy"] == (
                variant["tax_included_price_jpy"] * 65 + 99
            ) // 100


def test_quality_audit_fail_closed_counts_and_source_resource_only_policy() -> None:
    audit = load("deliverables/shijiu_initialization/stable_initialization_data_quality_audit.json")
    capacity = load("deliverables/shijiu_initialization/stable_initialization_capacity_estimate.json")
    assert audit["stable_catalog_product_count"] == 2435
    assert audit["stable_catalog_variant_count"] == 13742
    assert audit["stable_catalog_image_resource_count"] == 30145
    assert "DUPLICATE_PRODUCT_NAME" not in audit["quality_issue_counts"]
    assert audit["duplicate_good_name_identity_audit"]["duplicate_name_product_count"] == 1602
    assert audit["duplicate_good_name_identity_audit"][
        "all_duplicate_name_products_have_source_unique_complete_sku_sets"
    ] is True
    assert audit["missing_image_product_count"] == 0
    assert audit["missing_sku_product_count"] == 0
    assert audit["variant_identity_anomaly_product_count"] == 0
    assert capacity["status"] == "PLANNING_ONLY"
    assert capacity["write_status"] == WRITE_BLOCKED_STATUS
    assert capacity["estimated_unique_cos_resource_upload_count"] > 0
    assert capacity["safety"]["official_image_download_count"] == 0
    assert capacity["safety"]["shijiu_cos_upload_requests"] == 0
    price = load("deliverables/shijiu_initialization/price_outside_configured_range_audit.json")
    assert price["outside_range_variant_count"] == 0
    assert price["outside_range_product_count"] == 0
    assert price["guard_changed"] is True
    assert price["guard_change_scope"] == "SOURCE_ABSOLUTE_ELIGIBILITY_ONLY"
    assert price["price_change_absolute_and_relative_guards_changed"] is False
    assert price["automatic_import_release_count"] == 1
    assert price["released_product_numbers"] == ["13-6671-684"]


def _fixture_product() -> dict:
    number = "20-0001-001"
    sku = f"{number}00019999"
    image = {"url": "https://cdn.shopify.com/main.jpg", "width": 800, "height": 800}
    return {
        "product_number": number,
        "handle": number,
        "name": "テスト定番商品",
        "brand": "MIKI HOUSE",
        "product_type": "goods",
        "category": "goods",
        "tags": [],
        "description": "公式説明",
        "description_html": "<p>公式説明</p>",
        "product_url": f"https://www.mikihouse.co.jp/products/{number}",
        "active": True,
        "main_image": image,
        "ordered_images": [{"order": 1, "role": "main", "image": image}],
        "stability": {"status": "STABLE"},
        "variants": [{
            "stable_id": f"{number}::{sku}",
            "sku": sku,
            "active": True,
            "available_for_sale": True,
            "selected_options": [
                {"name": "カラー", "value": "赤"},
                {"name": "サイズ", "value": "F"},
            ],
            "color": "赤",
            "size": "F",
            "tax_included_price_jpy": 10000,
            "compare_at_price_jpy": None,
            "mini_program_price_jpy": 6500,
            "variant_image": image,
            "resolved_image": image,
        }],
    }


def test_catalog_hash_or_product_state_change_invalidates_plan() -> None:
    product = _fixture_product()
    product["source_content_sha256"] = "stable-product-hash"
    stable = {"catalog_kind": "MIKIHOUSE_STABLE_REGULAR_PRODUCT_POOL", "products": [product]}
    source = {
        "catalog_kind": "MIKIHOUSE_COMPLETE_STOREFRONT_SOURCE_SNAPSHOT",
        "complete_pagination_validated": True,
        "products": [copy.deepcopy(product)],
    }
    planned = {
        "product_number": product["product_number"],
        "source_content_sha256": product["source_content_sha256"],
        "source_snapshot_product_fingerprint_sha256": _source_product_fingerprint(product),
    }
    plan = {
        "freshness_guard": {
            "stable_catalog_logical_sha256": content_sha256(stable),
            "source_snapshot_logical_sha256": content_sha256(source),
        },
        "products": [planned],
    }
    mapping = {"products": {}}
    assert validate_plan_freshness(plan, stable, source, set(), mapping)["valid"] is True
    changed = copy.deepcopy(stable)
    changed["products"][0]["variants"][0]["available_for_sale"] = False
    result = validate_plan_freshness(plan, changed, source, set(), mapping)
    assert result["valid"] is False
    assert result["status"] == "STALE_FAIL_CLOSED_REBUILD_REQUIRED"
    assert result["mutation_allowed"] is False


def test_bound_mapping_forbids_create_in_freshness_guard() -> None:
    product = _fixture_product()
    product["source_content_sha256"] = "stable-product-hash"
    stable = {"catalog_kind": "MIKIHOUSE_STABLE_REGULAR_PRODUCT_POOL", "products": [product]}
    source = {
        "catalog_kind": "MIKIHOUSE_COMPLETE_STOREFRONT_SOURCE_SNAPSHOT",
        "complete_pagination_validated": True,
        "products": [copy.deepcopy(product)],
    }
    plan = {
        "freshness_guard": {
            "stable_catalog_logical_sha256": content_sha256(stable),
            "source_snapshot_logical_sha256": content_sha256(source),
        },
        "products": [{
            "product_number": product["product_number"],
            "source_content_sha256": product["source_content_sha256"],
            "source_snapshot_product_fingerprint_sha256": _source_product_fingerprint(product),
        }],
    }
    mapping = {"products": {product["product_number"]: {"shijiu_product_id": "90001"}}}
    result = validate_plan_freshness(plan, stable, source, set(), mapping)
    assert result["valid"] is False
    assert "ALREADY_MAPPED_CREATE_FORBIDDEN" in result["reasons"][-1]["products"][0]["reasons"]


def test_checkpoint_is_idempotent_and_batch_failure_is_isolated() -> None:
    plan = {
        "generated_at": "2026-09-05T00:00:00+00:00",
        "batches": [
            {"batch_id": "INIT-001", "sequence": 1, "product_numbers": ["20-0001-001"]},
            {"batch_id": "INIT-002", "sequence": 2, "product_numbers": ["20-0002-002"]},
        ],
        "products": [
            {"product_number": "20-0001-001", "batch_id": "INIT-001"},
            {"product_number": "20-0002-002", "batch_id": "INIT-002"},
        ],
    }
    first = initialize_checkpoint(plan)
    assert initialize_checkpoint(plan, first) == first
    frozen = freeze_initialization_batch(first, "INIT-001", "test failure")
    assert frozen["batches"]["INIT-001"]["status"] == "FROZEN_FAILED"
    assert frozen["products"]["20-0001-001"]["status"] == "FROZEN_WITH_BATCH"
    assert frozen["batches"]["INIT-002"]["status"] == "PLANNED"
    assert frozen["products"]["20-0002-002"]["status"] == "PLANNED"


def test_verified_mapping_hands_initialization_to_incremental_once() -> None:
    plan = {
        "generated_at": "2026-09-05T00:00:00+00:00",
        "batches": [{"batch_id": "INIT-001", "sequence": 1, "product_numbers": ["20-0001-001"]}],
        "products": [{"product_number": "20-0001-001", "batch_id": "INIT-001"}],
    }
    checkpoint = initialize_checkpoint(plan)
    state = {
        "products": {
            "20-0001-001": {"stability_status": "STABLE", "source_presence": "ACTIVE"}
        }
    }
    mapping = {
        "products": {
            "20-0001-001": {"source": SOURCE, "shijiu_product_id": "90001"}
        }
    }
    handed, source_state = handoff_initialized_product(
        checkpoint, state, mapping, "20-0001-001", "2026-09-05T01:00:00+00:00"
    )
    assert handed["products"]["20-0001-001"]["status"] == "INITIALIZED_HANDOFF_INCREMENTAL"
    assert handed["initialization_handoff_count"] == 1
    assert source_state["initialization_handoffs"]["20-0001-001"]["maintenance_mode"] == (
        "INCREMENTAL_EVENTS_ONLY_NO_FUTURE_CREATE"
    )
    assert handoff_initialized_product(
        handed, source_state, mapping, "20-0001-001", "2026-09-05T01:00:00+00:00"
    ) == (handed, source_state)


def test_protected_state_replan_is_zero_mutation_and_audited() -> None:
    audit = load("deliverables/shijiu_initialization/protected_state_change_audit.json")
    mapping = load("state/shijiu_mappings.json")
    checkpoint = load("state/mikihouse_initialization_checkpoint.json.gz")
    by_path = {row["path"]: row for row in audit["affected_state"]}
    for relative, row in by_path.items():
        digest = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        assert digest == row["after_sha256"]
    for row in audit["affected_protected_deliverables"]:
        digest = hashlib.sha256((ROOT / row["path"]).read_bytes()).hexdigest()
        assert digest == row["after_sha256"]
    for row in audit["unchanged_protected_artifacts"]:
        digest = hashlib.sha256((ROOT / row["path"]).read_bytes()).hexdigest()
        assert digest == row["sha256"]
    assert mapping["identity_contract"]["good_name_candidate_scope"] == (
        "exact only; never binding proof"
    )
    assert mapping["identity_contract"]["multiple_strong_matches"] == (
        "AMBIGUOUS_FAIL_CLOSED"
    )
    assert checkpoint["status"] == "FROZEN_PLANNING_ONLY"
    assert checkpoint["shijiu_mutation_count"] == 0
    assert checkpoint["writer_mutex_evidence_generated"] is False
    assert audit["historical_stale_plan"]["changed"] is False
