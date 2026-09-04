from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import mikihouse_luyao.shijiu_complex_import as complex_import
from mikihouse_luyao.shijiu_complex_import import (
    COMPLEX_WRITE_CONFIRMATION,
    ComplexLiveBatchRunner,
    UiContextReadClient,
    select_complex_batch,
)
from mikihouse_luyao.shijiu_import import load_mapping_state, map_product_to_shijiu
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


def source_product(number: str, name: str) -> dict:
    images = [
        {
            "order": index,
            "role": "main" if index == 1 else "product_gallery",
            "image": {"url": f"https://cdn.shopify.com/{number}-{index}.jpg", "width": 1000, "height": 1000},
        }
        for index in (1, 2)
    ]
    return {
        "product_number": number,
        "name": name,
        "active": True,
        "brand": "MIKI HOUSE",
        "category": {"name": "テスト"},
        "product_type": "通常商品",
        "tags": [],
        "description": "説明",
        "description_html": "<p>説明</p>",
        "main_image": images[0]["image"],
        "ordered_images": images,
        "variants": [
            {
                "sku": f"{number.replace('-', '')}{index}",
                "selected_options": [
                    {"name": "カラー", "value": color},
                    {"name": "サイズ", "value": size},
                ],
                "color": color,
                "size": size,
                "active": True,
                "available_for_sale": True,
                "tax_included_price_jpy": 2200 + index * 100,
                "mini_program_price_jpy": 1495 if index == 1 else 1560,
                "resolved_image": images[index - 1]["image"],
            }
            for index, (color, size) in enumerate((("赤", "80"), ("紺", "90")), start=1)
        ],
    }


def test_details_template_does_not_carry_source_links_into_formal_payload() -> None:
    product = source_product("20-9000-001", "外链说明商品")
    product["description"] = "官网说明 https://www.mikihouse.co.jp/collections/example 结束"
    item = map_product_to_shijiu(product, CATEGORY, excluded_product_numbers=exclusions())
    assert "https://www.mikihouse.co.jp" not in item["shijiu_payload_preview"]["good_details"]
    assert "官网说明" in item["shijiu_payload_preview"]["good_details"]


def mapping_state(items: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "source": "MIKIHOUSE",
        "target": "SHIJIU",
        "identity_contract": {
            "source_product_id": "MIKIHOUSE:<product_number>",
            "source_variant_id": "MIKIHOUSE:<product_number>:<variant SKU>",
            "backend_sku_code": "MIKI-<variant SKU>",
            "target_variant_identity": "shijiu_product_id + exact backend_sku_code",
            "shijiu_sku_id": "nullable; never guessed when official readback omits it",
            "product_match": "persisted post-create/readback mapping only",
            "product_name_matching": "forbidden",
            "legacy_reference_binding": "forbidden",
        },
        "products": {
            item["product_number"]: {
                "source": "MIKIHOUSE",
                "source_product_id": item["source_product_id"],
                "product_number": item["product_number"],
                "target_category_id": 294884,
                "source_present": True,
                "shijiu_product_id": None,
                "variants": {
                    row["source_variant_sku"]: {
                        "source": "MIKIHOUSE",
                        "source_variant_id": row["source_variant_id"],
                        "source_variant_sku": row["source_variant_sku"],
                        "backend_sku_code": row["backend_sku_code"],
                        "source_present": True,
                        "shijiu_sku_id": None,
                    }
                    for row in item["source_variants"]
                },
            }
            for item in items
        },
    }


class FakeTarget:
    def __init__(self) -> None:
        self.products: dict[str, dict] = {}


class FakeWriteClient:
    def __init__(self, target: FakeTarget) -> None:
        self.target = target
        self.requests: list[dict] = []

    def _record(self, path: str, semantic: str) -> None:
        self.requests.append({"path": path, "semantic_operation": semantic})

    def categories(self):
        self._record("/shopapi/Goodtype/typeindex", "read")
        return {"code": 1, "data": [{
            "id": 288338,
            "type_name": "母婴用品",
            "pid": 0,
            "children": [{"id": 294884, "type_name": "MikiHouse", "pid": 288338}],
        }]}

    def upload_image(self, source_url: str, *, confirmation: str):
        assert confirmation == COMPLEX_WRITE_CONFIRMATION
        self._record("/v1/cos/upload", "write")
        target = "https://cos.example.com/" + source_url.rsplit("/", 1)[-1]
        return target, {"code": 1, "data": {"url": target}}

    def create_product(self, payload: dict, *, confirmation: str):
        assert confirmation == COMPLEX_WRITE_CONFIRMATION
        self._record("/shopapi/Goods/newAddGood", "write")
        product_id = str(99000 + len(self.target.products) + 1)
        self.target.products[product_id] = copy.deepcopy(payload)
        return {"code": 200, "msg": "success", "data": []}


class FakeUiClient:
    def __init__(self, target: FakeTarget, *, fail_name: str = "") -> None:
        self.target = target
        self.fail_name = fail_name
        self.requests: list[dict] = []

    def safe_contract_summary(self):
        return {"credential_values_persisted": False, "filter_context": {"recommend": "2", "push": "2"}}

    def exact_name_candidates(self, name: str):
        self.requests.append({"path": "/shopapi/Goods/index", "semantic_operation": "read"})
        rows = [
            {"id": product_id, "good_name": payload["good_name"], "state": 1}
            for product_id, payload in self.target.products.items()
            if payload["good_name"] == name
        ]
        return rows, {
            "primary_identity_path": "UI-context exact good_name",
            "exact_good_name": name,
            "candidate_product_ids": [row["id"] for row in rows],
            "queries": [],
        }

    def product_detail(self, product_id: str):
        self.requests.append({"path": "/shopapi/goods/getFormatInfo", "semantic_operation": "read"})
        payload = copy.deepcopy(self.target.products[product_id])
        if payload["good_name"] == self.fail_name:
            payload["sku_info"][0]["sku_price"] = "999999.00"
        return {
            "code": 200,
            "msg": "success",
            "data": {"id": product_id, **payload},
        }


def five_items() -> list[dict]:
    return [
        map_product_to_shijiu(
            source_product(f"20-000{i}-00{i}", f"复杂商品{i}"),
            CATEGORY,
            excluded_product_numbers=exclusions(),
        )
        for i in range(1, 6)
    ]


def selection_for(items: list[dict]) -> dict:
    return {
        "products": [
            {"role": f"role-{index}", "product_number": item["product_number"], "payload_sha256": item["payload_sha256"]}
            for index, item in enumerate(items, start=1)
        ]
    }


def test_ui_context_query_changes_only_name_category_and_page() -> None:
    client = object.__new__(UiContextReadClient)
    client.base_pairs = [
        ("secret", "private"), ("page", "7"), ("page_size", "10"),
        ("good_type", ""), ("recommend", "2"), ("good_name", ""), ("push", "2"),
    ]
    pairs = client._query_pairs("精确名称", "294884", 1)
    assert [key for key, _ in pairs] == [key for key, _ in client.base_pairs]
    assert dict(pairs) == {
        "secret": "private", "page": "1", "page_size": "10", "good_type": "294884",
        "recommend": "2", "good_name": "精确名称", "push": "2",
    }


def test_selection_requires_all_five_roles_and_never_special(monkeypatch) -> None:
    roles = [
        ("footwear", 24, 4, 6, 42),
        ("apparel", 18, 6, 3, 66),
        ("goods", 6, 6, 1, 69),
        ("baby", 3, 3, 1, 31),
        ("goods", 3, 3, 1, 19),
    ]
    fake_pool = []
    for index, (classification, variants, colors, sizes, images) in enumerate(roles, start=1):
        number = f"20-100{index}-00{index}"
        fake_pool.append({
            "product": {"product_number": number, "name": f"候选{index}"},
            "mapped": {"product_number": number, "shijiu_payload_preview": {"good_name": f"候选{index}"}, "payload_sha256": str(index)},
            "classification": classification,
            "name_unique_in_source": True,
            "variant_count": variants,
            "available_variant_count": variants,
            "color_count": colors,
            "size_count": sizes,
            "image_count": images,
            "gallery_or_detail_image_count": images - colors,
        })
    monkeypatch.setattr(complex_import, "_candidate_pool", lambda *args: fake_pool)
    items, report = select_complex_batch({}, exclusions(), {}, CATEGORY)
    assert len(items) == 5
    assert [row["role"] for row in report["products"]] == [
        "multi_color_multi_size_footwear", "high_sku_apparel", "rich_gallery_and_details",
        "baby_product", "ordinary_goods",
    ]
    assert not ({item["product_number"] for item in items} & exclusions())


def test_complex_runner_verifies_each_product_and_persists_nullable_sku_identity(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(complex_import.time, "sleep", lambda _: None)
    items = five_items()
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(json.dumps(mapping_state(items)), encoding="utf-8")
    target = FakeTarget()
    write_client = FakeWriteClient(target)
    ui_client = FakeUiClient(target)
    runner = ComplexLiveBatchRunner(
        write_client,
        ui_client,
        items,
        exclusions(),
        CATEGORY,
        selection_for(items),
        checkpoint_path=tmp_path / "checkpoint.json",
        mapping_path=mapping_path,
        report_path=tmp_path / "report.json",
        readbacks_path=tmp_path / "readbacks.json",
        confirmation=COMPLEX_WRITE_CONFIRMATION,
    )
    report = runner.run()
    assert report["status"] == "COMPLETED"
    assert report["verified_product_count"] == 5
    assert report["verified_sku_count"] == 10
    assert report["request_counts"]["product_create"] == 5
    assert report["request_counts"]["image_upload"] == 10
    assert report["request_counts"]["update"] == 0
    mapping = load_mapping_state(mapping_path)
    assert all(mapping["products"][item["product_number"]]["shijiu_product_id"] for item in items)
    assert all(
        variant["shijiu_sku_id"] is None
        for item in items
        for variant in mapping["products"][item["product_number"]]["variants"].values()
    )


def test_complex_runner_stops_before_third_create_on_second_readback_mismatch(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(complex_import.time, "sleep", lambda _: None)
    items = five_items()
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(json.dumps(mapping_state(items)), encoding="utf-8")
    target = FakeTarget()
    write_client = FakeWriteClient(target)
    ui_client = FakeUiClient(target, fail_name="复杂商品2")
    runner = ComplexLiveBatchRunner(
        write_client, ui_client, items, exclusions(), CATEGORY, selection_for(items),
        checkpoint_path=tmp_path / "checkpoint.json", mapping_path=mapping_path,
        report_path=tmp_path / "report.json", readbacks_path=tmp_path / "readbacks.json",
        confirmation=COMPLEX_WRITE_CONFIRMATION,
    )
    with pytest.raises(LiveImportError):
        runner.run()
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["status"] == "STOPPED_ON_FIRST_ERROR"
    assert report["request_counts"]["product_create"] == 2
    assert [row["create_attempts"] for row in report["product_results"]] == [1, 1, 0, 0, 0]


def test_pdf_special_injection_fails_before_any_target_request(tmp_path: Path) -> None:
    items = five_items()
    special = exclusions()
    special.remove(next(iter(special)))
    special.add(items[0]["product_number"])
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(json.dumps(mapping_state(items)), encoding="utf-8")
    target = FakeTarget()
    write_client = FakeWriteClient(target)
    runner = ComplexLiveBatchRunner(
        write_client, FakeUiClient(target), items, special, CATEGORY, selection_for(items),
        checkpoint_path=tmp_path / "checkpoint.json", mapping_path=mapping_path,
        report_path=tmp_path / "report.json", readbacks_path=tmp_path / "readbacks.json",
        confirmation=COMPLEX_WRITE_CONFIRMATION,
    )
    with pytest.raises(LiveImportError, match="PDF_SPECIAL_LIST"):
        runner.run()
    assert write_client.requests == []


def test_post_stop_reconciliation_is_read_only_and_does_not_resume_batch(tmp_path: Path) -> None:
    items = five_items()
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(json.dumps(mapping_state(items)), encoding="utf-8")
    target = FakeTarget()
    write_client = FakeWriteClient(target)
    ui_client = FakeUiClient(target)
    runner = ComplexLiveBatchRunner(
        write_client, ui_client, items, exclusions(), CATEGORY, selection_for(items),
        checkpoint_path=tmp_path / "checkpoint.json", mapping_path=mapping_path,
        report_path=tmp_path / "report.json", readbacks_path=tmp_path / "readbacks.json",
        confirmation="",
    )
    first = runner.checkpoint["records"][items[0]["product_number"]]
    first["image_uploads"] = {
        upload["upload_reference"]: {
            "status": "UPLOADED",
            "target_url": "https://cos.example.com/" + upload["source_url"].rsplit("/", 1)[-1],
        }
        for upload in items[0]["image_upload_plan"]
    }
    first["create_attempts"] = 1
    first["create_response"] = {"code": 200, "msg": "success", "data": []}
    first["state"] = "STOPPED_ON_ERROR"
    runner.checkpoint["status"] = "STOPPED_ON_FIRST_ERROR"
    runner._persist()
    result = runner.reconcile_stopped_first_create()
    assert result["status"] == "BATCH_FROZEN_CREATE_NOT_VERIFIED"
    assert result["target_mutations"] == 0
    assert write_client.requests == []
    assert all(
        row["create_attempts"] == (1 if index == 0 else 0)
        for index, row in enumerate(runner.checkpoint["records"].values())
    )


def test_checked_in_complex_batch_evidence_is_fail_closed_and_sanitized() -> None:
    root = Path(__file__).resolve().parents[1]
    selection = json.loads((root / "config/shijiu_complex_live_batch.json").read_text())
    report = json.loads(
        (root / "deliverables/shijiu_import/complex_live_batch_report.json").read_text()
    )
    readiness = json.loads(
        (root / "deliverables/shijiu_import/complex_live_batch_readiness.json").read_text()
    )
    checkpoint = json.loads(
        (root / "state/shijiu_complex_live_batch_checkpoint.json").read_text()
    )
    mapping = json.loads((root / "state/shijiu_mappings.json").read_text())
    special = {
        row.split(",", 1)[0].lstrip("\ufeff")
        for row in (root / "special_skus_2026aw.csv").read_text(encoding="utf-8-sig").splitlines()[1:]
        if row
    }
    selected = [row["product_number"] for row in selection["products"]]
    assert len(selected) == len(set(selected)) == 5
    assert not (set(selected) & special)
    assert not (set(selected) & complex_import.PREVIOUSLY_TESTED_PRODUCTS)
    assert report["status"] == "STOPPED_ON_FIRST_ERROR"
    assert report["verified_product_count"] == 0
    assert report["uploaded_official_image_count"] == 42
    assert report["request_counts"] == {
        "read": 7, "write": 43, "image_upload": 42, "product_create": 1,
        "update": 0, "legacy_cleanup": 0,
    }
    assert [row["create_attempts"] for row in report["product_results"]] == [1, 0, 0, 0, 0]
    assert readiness["status"] == "BLOCKED_AFTER_FIRST_COMPLEX_CREATE_ANOMALY"
    assert readiness["next_batch_plan_generated"] is False
    assert readiness["next_batch_executed"] is False
    assert readiness["sensitive_values_included"] is False
    assert checkpoint["records"][selected[0]]["post_stop_delayed_ui_reconciliation"][
        "candidate_product_ids"
    ] == []
    assert mapping["products"][selected[0]]["shijiu_product_id"] is None
    assert [
        number for number, row in mapping["products"].items()
        if row.get("shijiu_product_id")
            ] == ["10-8375-578", "10-9129-792", "36-2001-572", "63-6602-492"]
