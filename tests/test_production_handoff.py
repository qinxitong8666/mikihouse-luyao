from __future__ import annotations

import copy
import gzip
import json
from pathlib import Path

import pytest

from mikihouse_luyao.production_handoff import (
    BLOCKED,
    MODE,
    READY,
    evaluate_handoff,
    preflight_pilot_resources,
)
from mikihouse_luyao.shijiu_import import content_sha256


ROOT = Path(__file__).resolve().parents[1]


def load(relative: str) -> dict:
    path = ROOT / relative
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def fixture_inputs() -> dict:
    source = load("output/storefront-stable/source_catalog.json")
    stable = load("deliverables/storefront_stable_catalog/stable_catalog.json.gz")
    pilot = load("deliverables/shijiu_initialization/stable_pilot_20_frozen_plan.json")
    batch = load(
        "deliverables/shijiu_initialization/stable_initialization_batch_plan.json.gz"
    )
    mapping = load("state/shijiu_mappings.json")
    richtext = load("config/shijiu_richtext_contract.json")
    duplicate = load("config/shijiu_duplicate_good_name_identity_contract.json")
    price = load("config/shijiu_price_guard.json")
    pilot["freshness_guard"]["price_policy_logical_sha256"] = content_sha256(price)
    historical = {
        row["product_number"]
        for row in batch["non_create_dispositions"]["historical_attempt_or_frozen"]
    }
    preflight = {
        "status": "PASSED",
        "pilot_product_count": 20,
        "resource_reference_count": sum(
            len(row["resource_manifest"]) for row in pilot["products"]
        ),
        "unique_source_url_count": len({
            image["source_url"]
            for row in pilot["products"]
            for image in row["resource_manifest"]
        }),
        "passed_unique_source_url_count": len({
            image["source_url"]
            for row in pilot["products"]
            for image in row["resource_manifest"]
        }),
        "failed_unique_source_url_count": 0,
        "evidence_logical_sha256": "a" * 64,
        "safety": {"shijiu_requests": 0, "shijiu_cos_upload_requests": 0},
    }
    return {
        "root": ROOT,
        "head_sha": "1" * 40,
        "branch": "main",
        "source_snapshot": source,
        "stable_catalog": stable,
        "stable_audit": load(
            "deliverables/storefront_stable_catalog/stable_pool_audit.json"
        ),
        "sync_cycle_report": load(
            "output/storefront-sync-cycle/sync_cycle_report.json"
        ),
        "pilot": pilot,
        "batch_plan": batch,
        "mapping": mapping,
        "special": set(
            row.strip().split(",")[0]
            for row in (ROOT / "special_skus_2026aw.csv").read_text().splitlines()[1:]
            if row.strip()
        ),
        "category": load("config/shijiu_category_map.json"),
        "richtext_contract": richtext,
        "duplicate_identity_contract": duplicate,
        "price_policy": price,
        "protocol": load("config/mikihouse_production_handoff_protocol.json"),
        "preflight": preflight,
        "historical_frozen": historical,
        "legacy_audit": load("deliverables/shijiu_import/legacy_reference_audit.json"),
    }


def test_preparation_only_ready_is_machine_auditable_and_has_zero_target_requests() -> None:
    result = evaluate_handoff(**fixture_inputs())
    assert result["status"] == MODE
    assert result["handoff_decision"] == READY
    assert result["machine_auditable_evidence_complete"] is True
    assert result["safety"] == {
        "shijiu_requests": 0,
        "shijiu_create_requests": 0,
        "shijiu_update_requests": 0,
        "shijiu_cos_upload_requests": 0,
        "shijiu_shelf_price_inventory_writes": 0,
        "writer_mutex_evidence_generated": False,
        "writer_mutex_evidence_generation_allowed": False,
        "legacy_286_touched": False,
        "pilot_execution_count": 0,
        "full_initialization_execution_count": 0,
    }
    assert "writer" not in " ".join(result["blocked_reason_codes"]).lower()


@pytest.mark.parametrize("stale_kind", ["stable", "source", "pilot"])
def test_any_stale_catalog_snapshot_or_pilot_hash_blocks(stale_kind: str) -> None:
    inputs = fixture_inputs()
    if stale_kind == "stable":
        inputs["stable_catalog"] = copy.deepcopy(inputs["stable_catalog"])
        inputs["stable_catalog"]["test_stale_marker"] = True
    elif stale_kind == "source":
        inputs["source_snapshot"] = copy.deepcopy(inputs["source_snapshot"])
        inputs["source_snapshot"]["test_stale_marker"] = True
    else:
        inputs["pilot"] = copy.deepcopy(inputs["pilot"])
        inputs["pilot"]["products"] = inputs["pilot"]["products"][:-1]
    result = evaluate_handoff(**inputs)
    assert result["handoff_decision"] == BLOCKED


@pytest.mark.parametrize(
    ("reason", "field"),
    [
        ("PDF_SPECIAL_LIST", None),
        ("WEB_EXCLUSIVE", "web_exclusive_product_numbers"),
        ("LIMITED_TIME_PRICE", "limited_time_price_product_numbers"),
        ("NON_SELLABLE_SERVICE_OR_ADDON", "non_sellable_service_or_addon_product_numbers"),
        ("REVIEW_REQUIRED_STABILITY", "review_required_product_numbers"),
    ],
)
def test_forbidden_reclassification_blocks_before_target_request(
    reason: str, field: str | None
) -> None:
    inputs = fixture_inputs()
    number = inputs["pilot"]["products"][0]["product_number"]
    if field is None:
        inputs["special"].add(number)
    else:
        inputs["stable_catalog"] = copy.deepcopy(inputs["stable_catalog"])
        inputs["stable_catalog"].setdefault("stability_exclusion", {}).setdefault(
            field, []
        ).append(number)
    result = evaluate_handoff(**inputs)
    assert result["handoff_decision"] == BLOCKED, reason
    assert result["safety"]["shijiu_requests"] == 0


@pytest.mark.parametrize("disposition", ["mapped", "historical"])
def test_mapped_and_historical_products_can_never_be_planned_create(
    disposition: str,
) -> None:
    inputs = fixture_inputs()
    number = inputs["pilot"]["products"][0]["product_number"]
    if disposition == "mapped":
        inputs["mapping"] = copy.deepcopy(inputs["mapping"])
        inputs["mapping"].setdefault("products", {})[number] = {
            "source": "MIKIHOUSE",
            "shijiu_product_id": "9359999",
        }
    else:
        inputs["historical_frozen"].add(number)
    result = evaluate_handoff(**inputs)
    assert result["handoff_decision"] == BLOCKED


def test_incomplete_crawl_blocks_and_never_becomes_inactive_evidence() -> None:
    inputs = fixture_inputs()
    inputs["source_snapshot"] = copy.deepcopy(inputs["source_snapshot"])
    inputs["source_snapshot"]["complete_pagination_validated"] = False
    result = evaluate_handoff(**inputs)
    assert result["handoff_decision"] == BLOCKED
    assert "COMPLETE_STOREFRONT_CRAWL" in result["blocked_reason_codes"]
    assert result["safety"]["shijiu_requests"] == 0


def test_same_inputs_are_idempotent() -> None:
    inputs = fixture_inputs()
    first = evaluate_handoff(**inputs)
    second = evaluate_handoff(**copy.deepcopy(inputs))
    assert first["decision_evidence_logical_sha256"] == second[
        "decision_evidence_logical_sha256"
    ]


def test_resource_preflight_is_source_only_and_deduplicates_urls() -> None:
    calls = []

    def fake_fetcher(url: str, **_: object) -> dict:
        calls.append(url)
        return {
            "status": "PASSED",
            "source_url_sha256": content_sha256(url),
            "source_host": "cdn.shopify.com",
            "final_url_sha256": content_sha256(url),
            "final_host": "cdn.shopify.com",
            "mime_type": "image/jpeg",
            "byte_count": 12,
            "content_sha256": "f" * 64,
            "decoded_format": "JPEG",
            "decoded_width": 100,
            "decoded_height": 100,
            "decoded_frame_count": 1,
            "http_attempt_count": 1,
            "shijiu_requests": 0,
            "shijiu_cos_upload_requests": 0,
        }

    pilot = {
        "products": [
            {
                "product_number": "10-0000-001",
                "resource_manifest": [
                    {
                        "source_url": "https://cdn.shopify.com/a.jpg",
                        "upload_reference": "one",
                        "order": 1,
                        "role": "main",
                    },
                    {
                        "source_url": "https://cdn.shopify.com/a.jpg",
                        "upload_reference": "two",
                        "order": 2,
                        "role": "variant",
                    },
                ],
            }
        ]
    }
    result = preflight_pilot_resources(
        pilot, {"entries": []}, workers=1, fetcher=fake_fetcher
    )
    assert result["status"] == "PASSED"
    assert result["resource_reference_count"] == 2
    assert result["unique_source_url_count"] == 1
    assert calls == ["https://cdn.shopify.com/a.jpg"]
    assert result["safety"]["shijiu_requests"] == 0
    assert result["safety"]["shijiu_cos_upload_requests"] == 0


def test_cli_exposes_no_live_write_or_gate_bypass_flags() -> None:
    script = (ROOT / "scripts/prepare_mikihouse_production_handoff.py").read_text()
    module = (ROOT / "src/mikihouse_luyao/production_handoff.py").read_text()
    assert "--live" not in script + module
    assert "--write" not in script + module
    assert "--force" not in script + module
    assert "--mutex" not in script + module
