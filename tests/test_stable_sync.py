from __future__ import annotations

import copy

import pytest

from mikihouse_luyao.stable_sync import (
    IMAGE_CHANGED,
    INVENTORY_CHANGED,
    NEW_PRODUCT,
    NEW_VARIANT,
    NO_CHANGE,
    PRICE_CHANGED,
    PRODUCT_INACTIVE,
    PRODUCT_REACTIVATED,
    REVIEW_EVENT,
    STABILITY_QUARANTINE,
    STABILITY_RESTORED,
    VARIANT_INACTIVE,
    VARIANT_REACTIVATED,
    IncompleteCrawlError,
    SyncCycleError,
    empty_sync_state,
    mark_event_consumed,
    plan_sync_cycle,
)
from mikihouse_luyao.stable_catalog import STABLE, assess_product_stability


CAPTURED = "2026-09-05T00:00:00+00:00"
GUARD = {
    "source": "MIKIHOUSE",
    "target": "SHIJIU",
    "minimum_tax_included_price_jpy": 1,
    "maximum_tax_included_price_jpy": 1_000_000,
    "maximum_absolute_change_jpy": 50_000,
    "maximum_relative_change_ratio": 0.5,
}


def special_set() -> set[str]:
    return {f"99-{index:04d}-999" for index in range(351)}


def product(
    number: str = "20-0001-001",
    *,
    price: int = 10_000,
    available: bool = True,
    image: str = "https://cdn.shopify.com/main.jpg",
    sku_suffix: str = "00019999",
) -> dict:
    sku = f"{number}{sku_suffix}"
    image_row = {"url": image, "width": 1000, "height": 1000, "alt_text": ""}
    return {
        "product_number": number,
        "handle": number,
        "name": "定番シューズ",
        "tags": ["shoes"],
        "description": "公式商品説明",
        "description_html": "<p>公式商品説明</p>",
        "product_url": f"https://www.mikihouse.co.jp/products/{number}",
        "active": True,
        "main_image": image_row,
        "ordered_images": [{"order": 1, "role": "main", "image": image_row}],
        "variants": [{
            "stable_id": f"{number}::{sku}",
            "sku": sku,
            "active": True,
            "available_for_sale": available,
            "selected_options": [
                {"name": "カラー", "value": "赤"},
                {"name": "サイズ", "value": "12cm"},
            ],
            "color": "赤",
            "size": "12cm",
            "tax_included_price_jpy": price,
            "compare_at_price_jpy": None,
            "mini_program_price_jpy": (price * 65 + 99) // 100,
            "variant_image": image_row,
            "resolved_image": image_row,
        }],
    }


def source(products: list[dict], *, complete: bool = True, captured: str = CAPTURED) -> dict:
    return {
        "schema_version": 1,
        "catalog_kind": "MIKIHOUSE_COMPLETE_STOREFRONT_SOURCE_SNAPSHOT",
        "captured_at": captured,
        "complete_pagination_validated": complete,
        "products": products,
    }


def stable_catalog(products: list[dict], special: set[str]) -> dict:
    stable = [
        copy.deepcopy(row)
        for row in products
        if assess_product_stability(row, special)["status"] == STABLE
    ]
    return {
        "catalog_kind": "MIKIHOUSE_STABLE_REGULAR_PRODUCT_POOL",
        "products": stable,
    }


def mapping_for(item: dict, *, bound: bool) -> dict:
    number = item["product_number"]
    return {
        "source": "MIKIHOUSE",
        "target": "SHIJIU",
        "products": {
            number: {
                "source": "MIKIHOUSE",
                "shijiu_product_id": "90001" if bound else None,
                "variants": {
                    row["sku"]: {"backend_sku_code": f"MIKI-{row['sku']}"}
                    for row in item["variants"]
                },
            }
        },
    }


def transition(before: list[dict], after: list[dict], *, bound: bool = True):
    special = special_set()
    mapped_item = before[0] if before else after[0]
    mapping = mapping_for(mapped_item, bound=bound)
    baseline, _, _, _ = plan_sync_cycle(
        empty_sync_state(), source(before), stable_catalog(before, special), special, mapping, GUARD,
        initialize_baseline=True,
    )
    return plan_sync_cycle(
        baseline,
        source(after, captured="2026-09-06T00:00:00+00:00"),
        stable_catalog(after, special),
        special,
        mapping,
        GUARD,
    )


def types(events: list[dict], number: str = "20-0001-001") -> list[str]:
    return [row["event_type"] for row in events if row["product_number"] == number]


def test_normal_price_change_uses_65_percent_jpy_and_no_change_is_explicit() -> None:
    before = product(price=10_000)
    after = product(price=11_001)
    _, _, events, actions = transition([before], [after])
    event = next(row for row in events if row["event_type"] == PRICE_CHANGED)
    assert event["after"]["target_price_jpy"] == 7_151
    assert next(row for row in actions if row["action_type"] == "UPDATE_PRICE")["execution_allowed"] is False
    _, _, unchanged, unchanged_actions = transition([before], [copy.deepcopy(before)])
    assert types(unchanged) == [NO_CHANGE]
    assert unchanged_actions == []


def test_new_stable_product_and_variant_plan_create_but_never_execute() -> None:
    item = product()
    special = special_set()
    state, report, events, actions = plan_sync_cycle(
        empty_sync_state(), source([item]), stable_catalog([item], special), special,
        mapping_for(item, bound=False), GUARD,
    )
    assert types(events) == [NEW_PRODUCT, NEW_VARIANT]
    assert [row["action_type"] for row in actions] == ["CREATE_PRODUCT"]
    assert all(row["execution_allowed"] is False for row in actions)
    variant = state["products"][item["product_number"]]["variants"][item["variants"][0]["sku"]]
    assert variant["source_variant_id"] == (
        f"MIKIHOUSE:{item['product_number']}:{item['variants'][0]['sku']}"
    )
    assert report["safety"]["shijiu_requests"] == 0


def test_new_non_sellable_addon_never_generates_create_or_variant_action() -> None:
    item = product("99-9999-000", price=550)
    item["name"] = "ギフトラッピング"
    item["product_type"] = "ギフトラッピング商品"
    item["tags"] = ["ギフトラッピング商品", "手数料商品"]
    special = special_set()
    state, _, events, actions = plan_sync_cycle(
        empty_sync_state(), source([item]), stable_catalog([item], special), special,
        mapping_for(item, bound=False), GUARD,
    )
    assert types(events, item["product_number"]) == [NO_CHANGE]
    assert events[0]["reason"] == "NEW_NON_SELLABLE_SERVICE_OR_ADDON_CREATE_SUPPRESSED"
    assert actions == []
    permanent = state["permanent_exclusions"]["NON_SELLABLE_SERVICE_OR_ADDON"]
    assert item["product_number"] in permanent["product_numbers"]


def test_stable_catalog_membership_mismatch_fails_closed() -> None:
    item = product()
    special = special_set()
    with pytest.raises(SyncCycleError, match="stable_catalog/source classification mismatch"):
        plan_sync_cycle(
            empty_sync_state(), source([item]),
            {"catalog_kind": "MIKIHOUSE_STABLE_REGULAR_PRODUCT_POOL", "products": []},
            special, mapping_for(item, bound=False), GUARD,
        )


def test_stale_stable_catalog_variant_data_fails_closed() -> None:
    item = product(price=10_000)
    stale = product(price=9_000)
    special = special_set()
    with pytest.raises(SyncCycleError, match="variant data is stale"):
        plan_sync_cycle(
            empty_sync_state(), source([item]), stable_catalog([stale], special), special,
            mapping_for(item, bound=False), GUARD,
        )


@pytest.mark.parametrize("before_available,after_available,target", [(True, False, 0), (False, True, 1)])
def test_inventory_boolean_changes_never_invent_exact_quantities(
    before_available: bool, after_available: bool, target: int
) -> None:
    before = product(available=before_available)
    after = product(available=after_available)
    _, _, events, _ = transition([before], [after])
    event = next(row for row in events if row["event_type"] == INVENTORY_CHANGED)
    assert event["after"]["target_stock"] == target


def test_new_and_missing_variant_events_use_exact_sku_identity() -> None:
    before = product()
    after = copy.deepcopy(before)
    extra = copy.deepcopy(before["variants"][0])
    extra["sku"] = f"{before['product_number']}00021200"
    extra["stable_id"] = f"{before['product_number']}::{extra['sku']}"
    after["variants"].append(extra)
    state, _, events, _ = transition([before], [after])
    assert NEW_VARIANT in types(events)
    missing_source = source([before], captured="2026-09-07T00:00:00+00:00")
    special = special_set()
    _, _, missing_events, _ = plan_sync_cycle(
        state, missing_source, stable_catalog([before], special), special,
        mapping_for(before, bound=True), GUARD,
    )
    assert VARIANT_INACTIVE in types(missing_events)


def test_product_inactive_and_reactivated_require_complete_crawls() -> None:
    target = product()
    sentinel = product("20-0001-999")
    state, _, inactive, _ = transition([target, sentinel], [sentinel])
    assert {PRODUCT_INACTIVE, VARIANT_INACTIVE}.issubset(types(inactive))
    special = special_set()
    mapping = mapping_for(target, bound=True)
    _, _, reactivated, _ = plan_sync_cycle(
        state,
        source([target, sentinel], captured="2026-09-07T00:00:00+00:00"),
        stable_catalog([target, sentinel], special),
        special,
        mapping,
        GUARD,
    )
    assert types(reactivated).count(PRODUCT_REACTIVATED) == 1
    assert types(reactivated).count(VARIANT_REACTIVATED) == 1
    with pytest.raises(IncompleteCrawlError):
        plan_sync_cycle(
            state,
            source([sentinel], complete=False),
            stable_catalog([sentinel], special),
            special,
            mapping,
            GUARD,
        )


def test_limited_time_price_quarantines_and_never_plans_promo_price() -> None:
    before = product(price=10_000)
    promo = product(price=8_000)
    promo["variants"][0]["compare_at_price_jpy"] = 10_000
    state, _, events, actions = transition([before], [promo])
    assert types(events) == [STABILITY_QUARANTINE]
    assert [row["action_type"] for row in actions] == ["DEACTIVATE_PRODUCT_STABILITY_QUARANTINE"]
    assert all(row["action_type"] != "UPDATE_PRICE" for row in actions)
    assert state["products"][before["product_number"]]["last_stable_variants"]


def test_limited_time_price_end_generates_guarded_stability_restored() -> None:
    normal = product(price=10_000)
    promo = product(price=8_000)
    promo["variants"][0]["compare_at_price_jpy"] = 10_000
    _, _, events, actions = transition([promo], [normal])
    assert types(events) == [STABILITY_RESTORED]
    assert actions[0]["action_type"] == "REACTIVATE_PRODUCT_STABILITY_RESTORED"
    assert events[0]["details"]["normal_price_assessments"][0]["target_price_jpy"] == 6_500


def test_web_exclusive_transition_quarantines_bound_but_new_web_never_creates() -> None:
    before = product()
    web = product()
    web["name"] = "【WEB限定】シューズ"
    _, _, events, actions = transition([before], [web])
    assert types(events) == [STABILITY_QUARANTINE]
    assert actions[0]["action_type"] == "DEACTIVATE_PRODUCT_STABILITY_QUARANTINE"
    special = special_set()
    _, _, new_events, new_actions = plan_sync_cycle(
        empty_sync_state(), source([web]), stable_catalog([web], special), special,
        mapping_for(web, bound=False), GUARD,
    )
    assert NEW_PRODUCT not in types(new_events)
    assert new_actions == []


def test_unconsumed_create_is_cancelled_if_product_becomes_web_exclusive() -> None:
    item = product()
    special = special_set()
    unbound = mapping_for(item, bound=False)
    state, _, _, first_actions = plan_sync_cycle(
        empty_sync_state(), source([item]), stable_catalog([item], special), special,
        unbound, GUARD,
    )
    assert any(row["action_type"] == "CREATE_PRODUCT" for row in first_actions)
    web = copy.deepcopy(item)
    web["name"] = "【WEB限定】シューズ"
    state, report, _, actions = plan_sync_cycle(
        state,
        source([web], captured="2026-09-06T00:00:00+00:00"),
        stable_catalog([web], special),
        special,
        unbound,
        GUARD,
    )
    assert actions == []
    assert state["pending_action_event_ids"] == []
    assert report["counts"]["pending_action_event_count"] == 0


def test_ambiguous_stability_transition_quarantines_bound_product_for_review() -> None:
    before = product()
    ambiguous = product()
    ambiguous["tags"] = ["webitem"]
    _, _, events, actions = transition([before], [ambiguous])
    assert types(events) == [STABILITY_QUARANTINE]
    assert events[0]["reason"] == "REVIEW_REQUIRED_STABILITY"
    assert actions[0]["action_type"] == "DEACTIVATE_PRODUCT_STABILITY_QUARANTINE"


def test_pdf_special_never_emits_any_event_or_shijiu_action() -> None:
    item = product("99-0000-999")
    special = special_set()
    assert item["product_number"] in special
    _, _, events, actions = plan_sync_cycle(
        empty_sync_state(), source([item]), stable_catalog([item], special), special,
        mapping_for(item, bound=True), GUARD,
    )
    assert events == []
    assert actions == []


def test_product_becoming_pdf_special_cancels_every_pending_action() -> None:
    item = product()
    original_special = special_set()
    unbound = mapping_for(item, bound=False)
    state, _, _, first_actions = plan_sync_cycle(
        empty_sync_state(), source([item]), stable_catalog([item], original_special),
        original_special, unbound, GUARD,
    )
    assert [row["action_type"] for row in first_actions] == ["CREATE_PRODUCT"]
    revised_special = set(original_special)
    revised_special.remove("99-0350-999")
    revised_special.add(item["product_number"])
    state, _, events, actions = plan_sync_cycle(
        state,
        source([item], captured="2026-09-06T00:00:00+00:00"),
        stable_catalog([item], revised_special),
        revised_special,
        unbound,
        GUARD,
    )
    assert events == []
    assert actions == []
    assert state["pending_action_event_ids"] == []


def test_abnormal_price_change_requires_review_and_has_no_update_action() -> None:
    before = product(price=10_000)
    after = product(price=100_000)
    _, _, events, actions = transition([before], [after])
    assert types(events) == [REVIEW_EVENT]
    assert events[0]["reason"] == "PRICE_GUARD_REJECTED"
    assert actions == []


def test_image_only_change_emits_only_image_changed() -> None:
    before = product()
    after = product(image="https://cdn.shopify.com/changed.jpg")
    _, _, events, _ = transition([before], [after])
    assert types(events) == [IMAGE_CHANGED]


def test_repeat_cycle_does_not_repeat_consumed_or_already_observed_change() -> None:
    before = product(price=10_000)
    after = product(price=11_000)
    state, _, events, actions = transition([before], [after])
    price_event = next(row for row in events if row["event_type"] == PRICE_CHANGED)
    original_action_id = next(row for row in actions if row["action_type"] == "UPDATE_PRICE")["action_id"]
    special = special_set()
    state, _, same_events, same_actions = plan_sync_cycle(
        state,
        source([after], captured="2026-09-07T00:00:00+00:00"),
        stable_catalog([after], special),
        special,
        mapping_for(after, bound=True),
        GUARD,
    )
    assert PRICE_CHANGED not in types(same_events)
    assert [row["action_id"] for row in same_actions] == [original_action_id]
    state = mark_event_consumed(state, price_event["event_id"])
    assert price_event["event_id"] not in state["pending_action_event_ids"]
    repeated_state, _, repeated, repeated_actions = plan_sync_cycle(
        state,
        source([after], captured="2026-09-08T00:00:00+00:00"),
        stable_catalog([after], special),
        special,
        mapping_for(after, bound=True),
        GUARD,
    )
    assert PRICE_CHANGED not in types(repeated)
    assert all(row["source_event_id"] != price_event["event_id"] for row in repeated_actions)
    assert price_event["event_id"] in repeated_state["consumed_event_ids"]


def test_unmapped_stability_restoration_becomes_new_product_not_reactivation() -> None:
    web = product()
    web["name"] = "WEB LIMITED 商品"
    normal = product()
    _, _, events, actions = transition([web], [normal], bound=False)
    assert types(events) == [NEW_PRODUCT]
    assert actions[0]["action_type"] == "CREATE_PRODUCT"
