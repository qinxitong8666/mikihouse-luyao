from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

import mikihouse_luyao.shijiu_staged_media_complete as complete
from mikihouse_luyao.shijiu_import import content_sha256, map_product_to_shijiu
from mikihouse_luyao.shijiu_live_import import (
    ContractMismatchError,
    is_official_mikihouse_image_url,
)


CATEGORY = {
    "id": 294884,
    "name": "MikiHouse",
    "parent_id": 288338,
    "parent_name": "母婴用品",
    "assignment_policy": "all_publishable_mikihouse_products",
}


def exclusions() -> set[str]:
    return {f"99-{index:04d}-999" for index in range(351)}


def source_product(number: str = "20-9000-002") -> dict:
    images = [
        {
            "order": index,
            "role": "main" if index == 1 else ("detail" if index == 6 else "product_gallery"),
            "image": {"url": f"https://cdn.shopify.com/{number}-{index}.jpg", "width": 900, "height": 900},
        }
        for index in range(1, 7)
    ]
    return {
        "product_number": number,
        "name": "完整预检测试商品",
        "active": True,
        "brand": "MIKI HOUSE",
        "category": {"name": "雑貨"},
        "product_type": "雑貨",
        "tags": [],
        "description": "説明",
        "main_image": images[0]["image"],
        "ordered_images": images,
        "variants": [
            {
                "sku": f"{number.replace('-', '')}{index}",
                "selected_options": [{"name": "カラー", "value": color}],
                "color": color,
                "size": "",
                "active": True,
                "available_for_sale": True,
                "tax_included_price_jpy": 2200,
                "mini_program_price_jpy": 1430,
                "resolved_image": images[index]["image"],
            }
            for index, color in enumerate(("赤", "紺"), start=1)
        ],
    }


def item() -> dict:
    return map_product_to_shijiu(source_product(), CATEGORY, excluded_product_numbers=exclusions())


def mapping_state(mapped: dict) -> dict:
    old = {
        "source": "MIKIHOUSE",
        "source_product_id": "MIKIHOUSE:10-8375-578",
        "product_number": "10-8375-578",
        "shijiu_product_id": "9358250",
        "variants": {},
    }
    return {
        "schema_version": 1,
        "source": "MIKIHOUSE",
        "target": "SHIJIU",
        "identity_contract": {"product_name_matching": "forbidden"},
        "products": {
            "10-8375-578": old,
            mapped["product_number"]: {
                "source": "MIKIHOUSE",
                "source_product_id": mapped["source_product_id"],
                "product_number": mapped["product_number"],
                "shijiu_product_id": None,
                "variants": {
                    row["source_variant_sku"]: {
                        "source": "MIKIHOUSE",
                        "source_variant_id": row["source_variant_id"],
                        "source_variant_sku": row["source_variant_sku"],
                        "backend_sku_code": row["backend_sku_code"],
                        "shijiu_sku_id": None,
                    }
                    for row in mapped["source_variants"]
                },
            },
        },
    }


class PreflightClient:
    def __init__(self, *, fail_at: int | None = None) -> None:
        self.requests: list[dict] = []
        self.calls = 0
        self.fail_at = fail_at

    def preflight_official_image(self, source_url: str):
        self.calls += 1
        if self.fail_at == self.calls:
            raise ContractMismatchError("fixture MIME failure")
        return {
            "source_url_sha256": hashlib.sha256(source_url.encode()).hexdigest(),
            "source_host": "cdn.shopify.com",
            "filename_extension": ".jpg",
            "response_mime_type": "image/jpeg",
            "detected_image_format": "JPEG",
            "byte_count": 1000,
            "content_sha256": str(self.calls),
            "width": 900,
            "height": 900,
            "shijiu_requests_sent": 0,
        }


class EmptyUi:
    requests: list[dict] = []


def build_runner(tmp_path: Path, monkeypatch, *, fail_at: int | None = None):
    monkeypatch.setattr(complete, "COMPLETE_PROTECTED_FILES", ("frozen.json",))
    (tmp_path / "frozen.json").write_text("{}")
    mapped = item()
    mapping = mapping_state(mapped)
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(json.dumps(mapping), encoding="utf-8")
    old_hash = content_sha256(mapping["products"]["10-8375-578"])
    selection = {
        "mode": complete.COMPLETE_MODE,
        "product": {"product_number": mapped["product_number"]},
        "stages": complete.stage_plan(mapped),
        "historical_prohibited_product_numbers": ["10-8375-578"],
        "protected_frozen_evidence": {"frozen.json": hashlib.sha256(b"{}").hexdigest()},
        "protected_existing_mapping_row_hashes": {"10-8375-578": old_hash},
    }
    client = PreflightClient(fail_at=fail_at)
    runner = complete.CompleteStagedMediaRunner(
        client,
        EmptyUi(),
        mapped,
        exclusions(),
        CATEGORY,
        selection,
        root=tmp_path,
        checkpoint_path=tmp_path / "checkpoint.json",
        mapping_path=mapping_path,
        report_path=tmp_path / "report.json",
        readbacks_path=tmp_path / "readbacks.json",
        confirmation="",
    )
    return runner, client


def test_official_host_matching_is_https_and_label_bounded() -> None:
    assert is_official_mikihouse_image_url("https://cdn.shopify.com/a.jpg")
    assert is_official_mikihouse_image_url("https://img.mksk.me/a.jpg")
    assert not is_official_mikihouse_image_url("http://cdn.shopify.com/a.jpg")
    assert not is_official_mikihouse_image_url("https://evilshopify.com/a.jpg")


def test_complete_resource_preflight_verifies_every_image_with_zero_shijiu_requests(
    tmp_path: Path, monkeypatch
) -> None:
    runner, client = build_runner(tmp_path, monkeypatch)
    result = runner.run_resource_preflight()
    assert result["status"] == "PASSED"
    assert result["verified_reference_count"] == 6
    assert result["shijiu_requests_sent"] == 0
    assert result["shijiu_write_requests_sent"] == 0
    assert client.requests == []
    assert runner.checkpoint["status"] == "READY_FOR_CREATE"


def test_resource_failure_blocks_before_all_shijiu_writes_and_cannot_create(
    tmp_path: Path, monkeypatch
) -> None:
    runner, client = build_runner(tmp_path, monkeypatch, fail_at=3)
    with pytest.raises(ContractMismatchError, match="MIME"):
        runner.run_resource_preflight()
    assert runner.checkpoint["status"] == "BLOCKED_RESOURCE_PREFLIGHT_ZERO_SHIJIU_WRITES"
    assert runner.checkpoint["resource_preflight"]["shijiu_write_requests_sent"] == 0
    assert client.requests == []
    with pytest.raises(complete.LiveImportError, match="preflight"):
        runner.run_next_step()
    assert client.requests == []


def test_frozen_pre_update_read_gate_is_normalized_without_a_target_request(
    tmp_path: Path, monkeypatch
) -> None:
    runner, client = build_runner(tmp_path, monkeypatch)
    stages = runner.checkpoint["stages"]
    stages[0].update({"state": "VERIFIED", "attempts": 1})
    snapshot = {
        "code": 200,
        "data": {
            "broadcast": "https://cdn0.19mini.com/a.jpg,https://cdn0.19mini.com/b.jpg",
            "good_detail_pics": "",
            "sku_info": [{"sku_code": "MIKI-A"}, {"sku_code": "MIKI-B"}],
        },
    }
    stages[1].update({
        "state": "FROZEN_ON_ANOMALY",
        "attempts": 0,
        "pre_update_getFormatInfo_snapshot": snapshot,
        "pre_update_snapshot_sha256": content_sha256(snapshot),
    })
    runner.checkpoint.update({
        "status": "FROZEN_ON_FIRST_ANOMALY",
        "stage_cursor": 1,
        "first_failed_state": {
            "stage": stages[1]["key"],
            "mutation_request_sent": False,
        },
        "request_ledger": [{
            "path": "/shopapi/Goods/newAddGood",
            "semantic_operation": "write",
            "operation": "native CREATE",
        }],
    })
    assert runner.normalize_frozen_pre_update_read_gate_evidence() is True
    failed = runner.checkpoint["first_failed_state"]
    assert failed["failure_scope"] == "PRE_UPDATE_READ_ONLY_GATE"
    assert failed["target_state_changed_by_failed_stage"] is False
    assert failed["last_persisted_target_state"] == {
        "broadcast_url_count": 2,
        "good_detail_pics_url_count": 0,
        "sku_count": 2,
        "pre_update_snapshot_sha256": content_sha256(snapshot),
    }
    assert client.requests == []
