from __future__ import annotations

import copy
import gzip
import json
from pathlib import Path

import pytest

from mikihouse_luyao.shijiu_duplicate_name_identity import (
    AMBIGUOUS,
    NOT_FOUND,
    UNIQUE_STRONG_MATCH,
    DuplicateNameIdentityError,
    analyze_duplicate_names,
    audit_price_outside_configured_range,
    resolve_duplicate_good_name_candidates,
)


ROOT = Path(__file__).resolve().parents[1]


def sku(code: str, price: int = 6500, spec: str = "赤,F") -> dict:
    return {"sku_code": code, "sku_price": price, "spec_name": spec}


def row(product_id: str, name: str = "パンツ") -> dict:
    return {"id": product_id, "good_name": name}


def detail(product_id: str, skus: list[dict], category: int = 294884) -> dict:
    return {
        "code": 1,
        "data": {
            "id": product_id,
            "good_type": category,
            "sku_info": copy.deepcopy(skus),
        },
    }


def test_complete_sku_set_uniquely_resolves_among_same_name_candidates() -> None:
    expected = [sku("MIKI-10-0001-00100019999"), sku("MIKI-10-0001-00100029999", 6600, "青,F")]
    candidates = [row("100"), row("101"), row("102")]
    details = {
        "100": detail("100", [expected[0]]),
        "101": detail("101", expected),
        "102": detail("102", [sku("FOREIGN-1")]),
    }
    result = resolve_duplicate_good_name_candidates(
        good_name="パンツ",
        sku_info=expected,
        candidate_rows=candidates,
        detail_by_product_id=details,
    )
    assert result["status"] == UNIQUE_STRONG_MATCH
    assert result["binding_allowed"] is True
    assert result["shijiu_product_id"] == "101"
    assert result["shijiu_sku_id"] is None
    assert result["strong_match_count"] == 1
    assert "SINGLE_SKU_OVERLAP" in result["forbidden_identity_inputs"]


def test_partial_or_single_sku_overlap_is_not_found_not_a_binding() -> None:
    expected = [sku("MIKI-A"), sku("MIKI-B")]
    result = resolve_duplicate_good_name_candidates(
        good_name="カバーオール",
        sku_info=expected,
        candidate_rows=[row("100", "カバーオール")],
        detail_by_product_id={"100": detail("100", [expected[0]])},
    )
    assert result["status"] == NOT_FOUND
    assert result["binding_allowed"] is False
    assert result["strong_match_count"] == 0


def test_two_complete_matches_are_ambiguous_and_fail_closed() -> None:
    expected = [sku("MIKI-A")]
    result = resolve_duplicate_good_name_candidates(
        good_name="トレーナー",
        sku_info=expected,
        candidate_rows=[row("100", "トレーナー"), row("101", "トレーナー")],
        detail_by_product_id={
            "100": detail("100", expected),
            "101": detail("101", expected),
        },
    )
    assert result["status"] == AMBIGUOUS
    assert result["binding_allowed"] is False
    assert result["shijiu_product_id"] is None
    assert result["strong_match_count"] == 2


@pytest.mark.parametrize(
    ("category", "price", "spec"),
    [(999, 6500, "赤,F"), (294884, 6501, "赤,F"), (294884, 6500, "青,F")],
)
def test_category_price_and_spec_are_required_auxiliary_conditions(
    category: int, price: int, spec: str
) -> None:
    expected = [sku("MIKI-A")]
    result = resolve_duplicate_good_name_candidates(
        good_name="セカンドベビーシューズ",
        sku_info=expected,
        candidate_rows=[row("100", "セカンドベビーシューズ")],
        detail_by_product_id={
            "100": detail("100", [sku("MIKI-A", price, spec)], category=category)
        },
    )
    assert result["status"] == NOT_FOUND
    assert result["binding_allowed"] is False


def test_every_exact_name_candidate_requires_get_format_info() -> None:
    with pytest.raises(DuplicateNameIdentityError, match="every exact-name candidate"):
        resolve_duplicate_good_name_candidates(
            good_name="パンツ",
            sku_info=[sku("MIKI-A")],
            candidate_rows=[row("100")],
            detail_by_product_id={},
        )


def test_real_stable_catalog_duplicate_sku_sets_are_naturally_unique() -> None:
    with gzip.open(
        ROOT / "deliverables/storefront_stable_catalog/stable_catalog.json.gz",
        "rt",
        encoding="utf-8",
    ) as stream:
        stable = json.load(stream)
    audit = analyze_duplicate_names(stable)
    assert audit["duplicate_name_group_count"] == 254
    assert audit["duplicate_name_product_count"] == 1602
    assert audit["maximum_group_size"] == 76
    assert audit["globally_duplicated_backend_sku_code_count"] == 0
    assert audit["identical_complete_backend_sku_set_group_count"] == 0
    assert audit["all_duplicate_name_products_have_source_unique_complete_sku_sets"] is True
    assert audit["theoretical_duplicate_name_review_release_count"] == 1602


def test_real_price_audit_classifies_zero_and_high_price_without_changing_guard() -> None:
    with gzip.open(
        ROOT / "deliverables/storefront_stable_catalog/stable_catalog.json.gz",
        "rt",
        encoding="utf-8",
    ) as stream:
        stable = json.load(stream)
    audit = audit_price_outside_configured_range(
        stable,
        minimum_tax_included_price_jpy=1,
        maximum_tax_included_price_jpy=1_000_000,
    )
    assert audit["outside_range_variant_count"] == 3
    assert audit["outside_range_product_count"] == 1
    assert audit["classification_counts"] == {
        "PLAUSIBLE_REAL_HIGH_PRICE_REQUIRES_MANUAL_APPROVAL": 3,
    }
    assert audit["guard_changed"] is False
    assert audit["automatic_import_release_count"] == 0


def test_persisted_shijiu_validation_is_read_only_and_proves_mapped_and_unmapped_cases() -> None:
    report = json.loads(
        (ROOT / "deliverables/shijiu_initialization/duplicate_good_name_shijiu_readonly_validation.json")
        .read_text(encoding="utf-8")
    )
    assert report["status"] == "READ_ONLY_DUPLICATE_GOOD_NAME_VALIDATION_PASSED"
    assert report["selected_group_count"] == 10
    assert report["mapped_unique_strong_match_count"] == 1
    assert report["unmapped_not_found_count"] == 325
    assert report["unexpected_outcome_count"] == 0
    assert {row["good_name"] for row in report["selected_groups"]} >= {
        "トレーナー",
        "カバーオール",
        "セカンドベビーシューズ",
    }
    safety = report["safety"]
    assert safety["shijiu_read_requests"] == 21
    assert safety["shijiu_create_requests"] == 0
    assert safety["shijiu_update_requests"] == 0
    assert safety["shijiu_cos_upload_requests"] == 0
    assert safety["shijiu_shelf_price_inventory_writes"] == 0
    assert safety["writer_mutex_evidence_generated"] is False
    assert safety["mapping_modified"] is False
    assert safety["legacy_products_modified"] == 0
    serialized = json.dumps(report, ensure_ascii=False).casefold()
    assert "authorization" not in serialized
    assert "cookie" not in serialized
    assert "pro_token" not in serialized
