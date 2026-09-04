from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from mikihouse_luyao.shijiu_high_sku_probe import (
    HIGH_SKU_PRODUCT_NUMBER,
    build_high_sku_diagnosis,
    build_staged_rich_media_plan,
)


def checkpoint(*, passed: bool) -> dict:
    return {
        "status": "COMPLETED" if passed else "STOPPED_ON_FIRST_ERROR",
        "records": {
            HIGH_SKU_PRODUCT_NUMBER: {
                "state": "READBACK_VERIFIED" if passed else "STOPPED_ON_ERROR",
                "create_attempts": 1,
                "image_uploads": {
                    str(index): {"status": "UPLOADED"} for index in range(6)
                },
                "mapping_persisted": passed,
                "readback": {"sku_count": 14 if passed else 0},
            }
        },
    }


def test_success_diagnosis_marks_14_sku_without_claiming_hard_limit() -> None:
    result = build_high_sku_diagnosis(checkpoint(passed=True), "audit-hash")
    assert result["fourteen_sku_scale_passed"] is True
    assert result["decision"] == (
        "SKU_SCALE_14_VERIFIED_RICH_MEDIA_SCALE_PRIMARY_REMAINING_FACTOR"
    )
    assert result["server_hard_limit_proven"] is False
    assert result["failed_13_9310_490_retried"] is False
    assert result["failed_00_4000_057_retried"] is False


def test_failed_probe_preserves_dual_factor_diagnosis() -> None:
    result = build_high_sku_diagnosis(checkpoint(passed=False), "audit-hash")
    assert result["fourteen_sku_scale_passed"] is False
    assert result["decision"] == "DUAL_FACTOR_UNRESOLVED_SKU_AND_RICH_MEDIA_SCALE"
    assert "sole cause" in result["explanation"]


def test_staged_rich_media_plan_is_not_executable_and_requires_verified_update() -> None:
    plan = build_staged_rich_media_plan()
    assert plan["status"] == "PLANNED_NOT_EXECUTED"
    assert plan["execution_authorized"] is False
    assert plan["update_requests_sent"] == 0
    assert len(plan["update_stages"]) == 3
    assert "repository-audited" in plan["update_contract_requirement"]
    assert "pre-update" in plan["rollback_prerequisite"]


def test_checked_in_high_sku_probe_is_single_create_strongly_verified_and_sanitized() -> None:
    root = Path(__file__).resolve().parents[1]
    selection = json.loads((root / "config/shijiu_high_sku_14_probe.json").read_text())
    checkpoint = json.loads(
        (root / "state/shijiu_high_sku_14_probe_checkpoint.json").read_text()
    )
    report = json.loads(
        (root / "deliverables/shijiu_import/high_sku_14_probe_report.json").read_text()
    )
    readbacks = json.loads(
        (root / "deliverables/shijiu_import/high_sku_14_probe_readbacks.json").read_text()
    )
    diagnosis = json.loads(
        (root / "deliverables/shijiu_import/high_sku_14_probe_diagnosis.json").read_text()
    )
    audit = json.loads(
        (root / "deliverables/shijiu_import/rich_media_capacity_empirical_audit.json").read_text()
    )
    plan = json.loads(
        (root / "deliverables/shijiu_import/staged_rich_media_update_plan.json").read_text()
    )
    mapping = json.loads((root / "state/shijiu_mappings.json").read_text())
    special = {
        row.split(",", 1)[0].lstrip("\ufeff")
        for row in (root / "special_skus_2026aw.csv").read_text(
            encoding="utf-8-sig"
        ).splitlines()[1:]
        if row
    }
    assert len(special) == 351
    assert HIGH_SKU_PRODUCT_NUMBER not in special
    assert selection["products"][0]["variant_count"] == 14
    assert selection["products"][0]["image_count"] == 6
    assert selection["products"][0]["detail_image_count"] == 4
    assert checkpoint["status"] == "COMPLETED"
    record = checkpoint["records"][HIGH_SKU_PRODUCT_NUMBER]
    assert record["state"] == "READBACK_VERIFIED"
    assert record["create_attempts"] == 1
    assert len(record["image_uploads"]) == 6
    assert record["mapping_persisted"] is True
    assert report["request_counts"] == {
        "read": 6,
        "write": 7,
        "image_upload": 6,
        "product_create": 1,
        "update": 0,
        "legacy_cleanup": 0,
    }
    assert report["fourteen_sku_scale_verified"] is True
    assert report["all_color_size_pairs_verified_via_exact_specification"] is True
    assert readbacks["verified_sku_count"] == 14
    assert len({row["backend_sku_code"] for row in readbacks["results"][0]["skus"]}) == 14
    assert all(row["price_jpy"] == 8580 for row in readbacks["results"][0]["skus"])
    assert all(row["stock"] == 1 for row in readbacks["results"][0]["skus"])
    assert all(
        row["color_size_verified_via_exact_specification"] is True
        for row in readbacks["results"][0]["skus"]
    )
    mapped = mapping["products"][HIGH_SKU_PRODUCT_NUMBER]
    assert mapped["shijiu_product_id"] == record["shijiu_product_id"]
    assert len(mapped["variants"]) == 14
    assert all(row["shijiu_sku_id"] is None for row in mapped["variants"].values())
    assert diagnosis["fourteen_sku_scale_passed"] is True
    assert plan["status"] == "PLANNED_NOT_EXECUTED"
    assert plan["update_requests_sent"] == 0
    assert audit["status"] == "COMPLETED_READ_ONLY"
    assert audit["request_counts"]["write"] == 0
    assert audit["scope"]["unique_detail_product_count"] == 328
    assert audit["interpretation"]["server_hard_limit"] == (
        "NOT_PROVEN_AND_NOT_INFERRED_FROM_OBSERVED_MAXIMA"
    )
    for label, path in {
        "original_complex_batch_checkpoint_sha256": root / "state/shijiu_complex_live_batch_checkpoint.json",
        "prior_bisection_selection_sha256": root / "config/shijiu_complexity_bisection_batch.json",
        "prior_bisection_checkpoint_sha256": root / "state/shijiu_complexity_bisection_checkpoint.json",
        "prior_bisection_report_sha256": root / "deliverables/shijiu_import/complexity_bisection_report.json",
    }.items():
        assert selection["protected_frozen_evidence"][label] == hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    serialized = json.dumps(
        {
            "selection": selection,
            "report": report,
            "diagnosis": diagnosis,
            "audit": audit,
            "plan": plan,
        },
        ensure_ascii=False,
    ).casefold()
    assert re.search(r'"(?:token|secret|cookie|authorization)"\s*:', serialized) is None
