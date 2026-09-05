from __future__ import annotations

import gzip
import json
from collections import Counter
from pathlib import Path

from mikihouse_luyao.sellable_review_audit import observe_official_product_page
from mikihouse_luyao.stable_catalog import PERMANENT_NON_SELLABLE_PRODUCT_NUMBERS


ROOT = Path(__file__).resolve().parents[1]


def load(path: str) -> dict:
    target = ROOT / path
    opener = gzip.open if target.suffix == ".gz" else open
    with opener(target, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def test_non_sellable_manifest_matches_code_and_official_evidence_report() -> None:
    config = load("config/mikihouse_non_sellable_service_or_addon.json")
    report = load(
        "deliverables/storefront_stable_catalog/sellable_review_resolution_audit.json"
    )
    expected = set(config["product_numbers"])
    assert expected == set(PERMANENT_NON_SELLABLE_PRODUCT_NUMBERS)
    assert len(expected) == 27
    assert set(report["non_sellable_service_or_addon_product_numbers"]) == expected
    assert report["official_page_audit_product_count"] == 28
    assert report["non_sellable_service_or_addon_count"] == 27
    assert report["remaining_review_required_product_numbers"] == ["13-6671-684"]
    assert report["stable_non_sellable_leak_product_numbers"] == []
    assert report["final_sellable_stable_product_count"] == 2434
    resolution = report["prior_27_review_resolution"]
    assert resolution["zero_price_source_data_product_count"] == 20
    assert resolution["zero_price_products_confirmed_non_sellable_count"] == 20
    assert resolution["zero_price_independent_sellable_products_remaining_review_count"] == 0
    assert resolution["missing_media_product_count"] == 7
    assert resolution["missing_media_confirmed_non_sellable_count"] == 7
    assert resolution["sellable_missing_media_remaining_review_count"] == 0
    assert resolution["missing_media_overlap_zero_price_product_numbers"] == [
        "00-9999-002"
    ]

    rows = {row["product_number"]: row for row in report["products"]}
    assert rows["99-9999-000"]["was_in_prior_27_initialization_reviews"] is False
    type_counts = Counter(
        rows[number]["official_storefront_evidence"]["product_type"]
        for number in expected
    )
    assert type_counts == {
        "名入れ代商品": 7,
        "ノベルティ商品": 14,
        "メッセージカード商品": 5,
        "ギフトラッピング商品": 1,
    }
    for number in expected:
        row = rows[number]
        page = row["official_page_evidence"]
        evidence = row["classification_evidence"]
        assert page["http_status"] == 200
        assert page["final_url_matches_product"] is True
        assert page["exact_product_number_marker_present"] is True
        assert page["all_exact_variant_skus_present"] is True
        assert page["raw_html_committed"] is False
        assert len(page["response_sha256"]) == 64
        assert evidence["classification_does_not_depend_on_zero_price"] is True
        assert row["disposition"] == "EXCLUDED_NON_SELLABLE_SERVICE_OR_ADDON"


def test_high_price_product_is_verified_but_guard_stays_unchanged() -> None:
    report = load(
        "deliverables/storefront_stable_catalog/sellable_review_resolution_audit.json"
    )
    high = report["high_price_verification"]
    assert high["verified_real_high_price_product"] is True
    assert high["variant_count"] == 3
    assert high["variant_tax_included_price_jpy"] == [1_430_000] * 3
    assert high["variant_compare_at_price_jpy"] == [1_430_000] * 3
    assert high["target_price_jpy"] == 929_500
    assert high["structured_compare_at_promotion_present"] is False
    assert high["explicit_sale_or_limited_price_signal_present"] is False
    assert high["official_page_has_normal_purchase_surface"] is True
    assert high["official_page_visible_promotion_markers"] == []
    assert high["official_page_visible_promotion_absent"] is True
    assert high["source_price_upper_bound_current_jpy"] == 1_000_000
    assert high["source_price_upper_bound_recommended_jpy"] == 2_000_000
    assert high["guard_changed_this_round"] is False


def test_visible_promotion_parser_ignores_generic_shopify_sale_css_classes() -> None:
    number = "13-6671-684"
    product = {
        "product_number": number,
        "name": "高価商品",
        "product_type": "通常商品",
        "variants": [{"sku": f"{number}00223400", "tax_included_price_jpy": 1_430_000}],
    }
    raw = f"""
      <link href=\"https://www.mikihouse.co.jp/products/{number}\">
      <section>商品番号: {number}
        <h1>高価商品</h1><div>通常商品</div>
        <div class=\"price__sale\"><span class=\"price-item--sale\">¥1,430,000</span></div>
        <form action=\"/cart/add\"><span>{number}00223400</span>
          <button name=\"add\">カートに入れる</button>
        </form>
      </section>
    """.encode()
    result = observe_official_product_page(
        product,
        raw,
        final_url=f"https://www.mikihouse.co.jp/products/{number}",
        http_status=200,
    )
    assert result["product_section_promotional_text_markers"] == []
    assert result["cart_add_form_present"] is True
    assert result["any_enabled_add_button"] is True


def test_resolution_summary_and_initialization_plan_are_zero_leak_planning_only() -> None:
    summary = load(
        "deliverables/shijiu_initialization/sellable_initialization_resolution_report.json"
    )
    plan = load("deliverables/shijiu_initialization/stable_initialization_batch_plan.json.gz")
    assert summary["status"] == "COMPLETED_PLANNING_ONLY_SELLABLE_INITIALIZATION_RESOLUTION"
    assert summary["final_sellable_stable_product_count"] == 2434
    assert summary["non_sellable_service_or_addon"]["count"] == 27
    assert summary["remaining_initialization_review_required"]["count"] == 1
    assert summary["remaining_initialization_review_required"]["products"][0][
        "product_number"
    ] == "13-6671-684"
    assert summary["initialization_counts"] == plan["counts"]
    assert summary["pilot_20"] == {
        "product_count": 20,
        "coverage": {"apparel": 5, "baby": 5, "footwear": 5, "goods": 5},
        "all_still_valid": True,
        "execution_count": 0,
    }
    leak = summary["initialization_plan_leak_audit"]
    assert leak["passed"] is True
    assert leak["all_forbidden_leak_product_numbers"] == []
    assert all(
        not values
        for key, values in leak.items()
        if key.endswith("_product_numbers")
    )
    safety = summary["safety"]
    assert safety["shijiu_requests"] == 0
    assert safety["shijiu_create_requests"] == 0
    assert safety["shijiu_update_requests"] == 0
    assert safety["shijiu_cos_upload_requests"] == 0
    assert safety["writer_mutex_evidence_generated"] is False
