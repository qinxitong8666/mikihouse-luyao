from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

import mikihouse_luyao.shijiu_complexity_bisection as bisection
from mikihouse_luyao.shijiu_complexity_bisection import (
    BISECTION_MODE,
    BISECTION_WRITE_CONFIRMATION,
    ComplexityBisectionRunner,
    audit_wawu_multisku_evidence,
    audit_shijiu_legacy_multisku_evidence,
    build_bisection_diagnosis,
    build_orphan_asset_register,
    payload_complexity_metrics,
    select_bisection_batch,
)
from mikihouse_luyao.shijiu_import import content_sha256
from mikihouse_luyao.shijiu_live_import import LiveImportError


CATEGORY = {
    "id": 294884,
    "name": "MikiHouse",
    "parent_id": 288338,
    "parent_name": "母婴用品",
    "assignment_policy": "all_publishable_mikihouse_products",
}


def exclusions() -> set[str]:
    return {f"99-{index:04d}-999" for index in range(351)}


def candidate(number: str, variants: int, images: int, details: int, *, unique: bool = True) -> dict:
    mapped = {
        "product_number": number,
        "source_product_id": f"MIKIHOUSE:{number}",
        "payload_sha256": f"hash-{number}",
        "shijiu_payload_preview": {"good_name": f"商品{number}"},
    }
    return {
        "product": {"product_number": number},
        "mapped": mapped,
        "name_unique_in_source": unique,
        "variant_count": variants,
        "available_variant_count": variants,
        "color_count": 2,
        "size_count": variants,
        "image_count": images,
        "gallery_or_detail_image_count": images - 1,
        "detail_image_count": images - 1,
        "good_details_character_count": details,
    }


def test_payload_metrics_quantify_utf8_specs_skus_and_images_without_values() -> None:
    payload = {
        "good_name": "测试商品",
        "spec_name": [
            {"spec_name": "颜色", "son_name": [{"spec_name": "赤"}, {"spec_name": "紺"}]},
            {"spec_name": "尺码", "son_name": [{"spec_name": "80"}]},
        ],
        "sku_info": [{"sku_code": "A"}, {"sku_code": "B"}],
        "master_graph": "https://cos.example/a.jpg",
        "broadcast": "https://cos.example/a.jpg,https://cos.example/b.jpg",
        "good_detail_pics": "https://cos.example/b.jpg",
        "good_details": '<p><img src="https://cos.example/b.jpg"></p>',
    }
    metrics = payload_complexity_metrics(payload, token="token-value", secret="secret-value")
    assert metrics["sku_info_count"] == 2
    assert metrics["spec_dimension_count"] == 2
    assert metrics["spec_option_count_total"] == 3
    assert metrics["broadcast"]["url_count"] == 2
    assert metrics["good_details"]["image_tag_count"] == 1
    assert metrics["good_detail_pics"]["url_count"] == 1
    assert metrics["wire_body_utf8_byte_count"] > metrics["business_payload_utf8_byte_count"]
    serialized = json.dumps(metrics, ensure_ascii=False)
    assert "token-value" not in serialized and "secret-value" not in serialized


def test_selection_is_ordered_image_then_sku_probe_and_excludes_old_batch(monkeypatch) -> None:
    pool = [
        candidate("20-0001-001", 4, 60, 5000),
        candidate("20-0002-002", 3, 80, 6000),
        candidate("20-0003-003", 14, 9, 900),
        candidate("20-0004-004", 18, 6, 700, unique=False),
        candidate("13-9310-490", 2, 999, 9999),
    ]
    monkeypatch.setattr(bisection, "_candidate_pool", lambda *args: pool[:-1])
    items, report = select_bisection_batch({}, exclusions(), {}, CATEGORY)
    assert [item["product_number"] for item in items] == ["20-0002-002", "20-0004-004"]
    assert [row["role"] for row in report["products"]] == [
        "IMAGE_DETAIL_SCALE_2_TO_4_VARIANTS",
        "SKU_SCALE_12_TO_24_VARIANTS",
    ]
    assert report["second_requires_first_strong_readback_and_mapping"] is True
    assert "13-9310-490" in report["hard_prohibited_products"]


def test_wawu_reference_requires_all_created_rows_uniquely_read_back(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    value = {
        "import": {
            "created": 2,
            "failed": 0,
            "compact_items": [
                {"status": "VERIFIED_DONE", "reason": "CREATED_UNIQUE_READBACK", "write_count": 1, "sku_count": 3},
                {"status": "VERIFIED_DONE", "reason": "CREATED_UNIQUE_READBACK", "write_count": 1, "sku_count": 11},
            ],
        }
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    result = audit_wawu_multisku_evidence(path)
    assert result["maximum_verified_sku_count_per_created_product"] == 11
    assert result["verified_created_sku_count_total"] == 14
    value["import"]["compact_items"][1]["reason"] = "UNVERIFIED"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(LiveImportError):
        audit_wawu_multisku_evidence(path)


def test_legacy_reference_only_proves_readable_not_current_create_scale(tmp_path: Path) -> None:
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({
        "classification": "legacy_reference_only",
        "passed": True,
        "sample_size": 2,
        "sample_schemas": [{"detail_sku_count": 9}, {"detail_sku_count": 24}],
        "sku_binding_attempted": False,
    }), encoding="utf-8")
    result = audit_shijiu_legacy_multisku_evidence(path)
    assert result["maximum_readable_sku_count_per_existing_product"] == 24
    assert result["proves_current_canonical_CREATE_acceptance"] is False


def test_orphan_register_requires_exactly_42_complete_uploads(tmp_path: Path) -> None:
    checkpoint = {
        "records": {
            "13-9310-490": {
                "image_uploads": {
                    f"MIKIHOUSE:13-9310-490:IMAGE:{index:03d}": {
                        "order": index,
                        "role": "product_gallery",
                        "source_url_sha256": str(index),
                        "target_url": f"https://cos.example/{index}.jpg",
                        "status": "UPLOADED",
                    }
                    for index in range(1, 43)
                }
            }
        }
    }
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(checkpoint), encoding="utf-8")
    result = build_orphan_asset_register(path)
    assert result["asset_count"] == 42
    assert result["delete_allowed"] is False
    assert result["reupload_allowed_in_this_batch"] is False
    assert result["assets"][0]["target_url_sha256"] == hashlib.sha256(
        result["assets"][0]["target_url"].encode()
    ).hexdigest()


@pytest.mark.parametrize(
    ("states", "attempts", "decision"),
    [
        (["READBACK_VERIFIED", "READBACK_VERIFIED"], [1, 1], "NEITHER_SCALE_ALONE_EXPLAINS_13_9310_490_FAILURE"),
        (["STOPPED_ON_ERROR", "PLANNED"], [1, 0], "IMAGE_OR_DETAIL_SCALE_SUSPECTED_SKU_PROBE_NOT_RUN"),
        (["READBACK_VERIFIED", "STOPPED_ON_ERROR"], [1, 1], "SKU_SCALE_SUSPECTED"),
    ],
)
def test_diagnosis_uses_sequential_probe_outcomes(states, attempts, decision) -> None:
    checkpoint = {
        "records": {
            str(index): {"state": state, "create_attempts": attempt}
            for index, (state, attempt) in enumerate(zip(states, attempts))
        }
    }
    assert build_bisection_diagnosis(checkpoint)["decision"] == decision


class NoRequestWriteClient:
    def __init__(self) -> None:
        self.requests = []

    def categories(self):
        raise AssertionError("prohibited identity must fail before target discovery")


class NoRequestUiClient:
    requests: list[dict] = []

    def safe_contract_summary(self):
        return {"credential_values_persisted": False}


def test_bisection_runner_hard_blocks_failed_reference_before_target_request(tmp_path: Path) -> None:
    numbers = ["13-9310-490", "20-0004-004"]
    items = []
    products = {}
    for number in numbers:
        payload = {"good_name": number}
        item = {
            "product_number": number,
            "source_product_id": f"MIKIHOUSE:{number}",
            "payload_sha256": content_sha256(payload),
            "source_variants": [],
            "image_upload_plan": [],
            "shijiu_payload_preview": payload,
        }
        items.append(item)
        products[number] = {
            "source": "MIKIHOUSE",
            "shijiu_product_id": None,
            "variants": {},
        }
    mapping = {"schema_version": 1, "source": "MIKIHOUSE", "target": "SHIJIU", "products": products}
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(json.dumps(mapping), encoding="utf-8")
    selection = {
        "products": [
            {"product_number": item["product_number"], "payload_sha256": item["payload_sha256"]}
            for item in items
        ]
    }
    client = NoRequestWriteClient()
    runner = ComplexityBisectionRunner(
        client,
        NoRequestUiClient(),
        items,
        exclusions(),
        CATEGORY,
        selection,
        checkpoint_path=tmp_path / "checkpoint.json",
        mapping_path=mapping_path,
        report_path=tmp_path / "report.json",
        readbacks_path=tmp_path / "readbacks.json",
        confirmation=BISECTION_WRITE_CONFIRMATION,
    )
    with pytest.raises(LiveImportError, match="prohibited"):
        runner.run()
    assert client.requests == []
    assert json.loads((tmp_path / "checkpoint.json").read_text())["mode"] == BISECTION_MODE


def test_checked_in_bisection_evidence_is_frozen_fail_closed_and_sanitized() -> None:
    root = Path(__file__).resolve().parents[1]
    selection = json.loads(
        (root / "config/shijiu_complexity_bisection_batch.json").read_text()
    )
    checkpoint_path = root / "state/shijiu_complexity_bisection_checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text())
    report = json.loads(
        (root / "deliverables/shijiu_import/complexity_bisection_report.json").read_text()
    )
    diagnosis = json.loads(
        (root / "deliverables/shijiu_import/complexity_bisection_diagnosis.json").read_text()
    )
    orphan = json.loads(
        (root / "deliverables/shijiu_import/orphan_cos_assets_13_9310_490.json").read_text()
    )
    mapping = json.loads((root / "state/shijiu_mappings.json").read_text())
    special = {
        row.split(",", 1)[0].lstrip("\ufeff")
        for row in (root / "special_skus_2026aw.csv").read_text(encoding="utf-8-sig").splitlines()[1:]
        if row
    }
    numbers = [row["product_number"] for row in selection["products"]]
    assert numbers == ["00-4000-057", "63-6602-492"]
    assert not set(numbers) & special
    assert not set(numbers) & bisection.ALL_PREVIOUS_CREATE_PRODUCTS
    assert report["status"] == "STOPPED_ON_FIRST_ERROR"
    assert report["request_counts"]["image_upload"] == 74
    assert report["request_counts"]["product_create"] == 1
    assert report["request_counts"]["update"] == 0
    assert report["request_counts"]["legacy_cleanup"] == 0
    assert [row["create_attempts"] for row in report["product_results"]] == [1, 0]
    assert [row["uploaded_image_count"] for row in report["product_results"]] == [74, 0]
    assert checkpoint["status"] == "STOPPED_ON_FIRST_ERROR"
    assert diagnosis["decision"] == "IMAGE_OR_DETAIL_SCALE_SUSPECTED_SKU_PROBE_NOT_RUN"
    assert diagnosis["second_probe_permanently_not_executed_after_initial_readback_failure"] is True
    assert diagnosis["target_mutations_in_reconciliation"] == 0
    assert diagnosis["decision_basis"]["known_verified_CREATE_sku_maximum"] == 11
    assert diagnosis["decision_basis"]["known_existing_readable_sku_maximum"] == 24
    assert orphan["asset_count"] == 42
    assert orphan["reupload_allowed_in_this_batch"] is False
    assert mapping["products"][numbers[0]]["shijiu_product_id"] is None
    assert checkpoint["records"][numbers[1]]["state"] == "PLANNED"
    assert checkpoint["records"][numbers[1]]["create_attempts"] == 0
    assert checkpoint["records"][numbers[1]]["image_uploads"] == {}
    # 63-6602-492 was later authorized and verified in a wholly independent
    # one-product checkpoint; the old bisection record remains byte-frozen.
    assert mapping["products"][numbers[1]]["shijiu_product_id"] == "9358241"
    assert [
        number for number, row in mapping["products"].items() if row.get("shijiu_product_id")
    ] == ["36-2001-572", "63-6602-492"]
    serialized = json.dumps(
        {"selection": selection, "report": report, "diagnosis": diagnosis, "orphan": orphan}
    ).casefold()
    assert "/users/" not in serialized
    assert re.search(r'"(?:token|secret|cookie)"\s*:', serialized) is None
