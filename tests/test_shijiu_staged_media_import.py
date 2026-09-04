from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

import mikihouse_luyao.shijiu_staged_media_import as staged
from mikihouse_luyao.shijiu_import import map_product_to_shijiu
from mikihouse_luyao.shijiu_live_import import (
    OFFICIAL_MIKIHOUSE_IMAGE_HOST_SUFFIXES,
    ContractMismatchError,
    validate_canonical_update_payload,
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


def rich_product(number: str = "20-9000-001", image_count: int = 27) -> dict:
    images = [
        {
            "order": index,
            "role": "main" if index == 1 else ("detail" if index > image_count - 3 else "product_gallery"),
            "image": {
                "url": f"https://cdn.shopify.com/{number}-{index}.jpg",
                "width": 1200,
                "height": 1200,
            },
        }
        for index in range(1, image_count + 1)
    ]
    return {
        "product_number": number,
        "name": "唯一富媒体商品",
        "active": True,
        "brand": "MIKI HOUSE",
        "category": {"name": "雑貨"},
        "product_type": "雑貨",
        "tags": [],
        "description": "テスト説明",
        "description_html": "<p>テスト説明</p>",
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
                "tax_included_price_jpy": 2200,
                "mini_program_price_jpy": 1430,
                "resolved_image": images[18 + index]["image"],
            }
            for index, (color, size) in enumerate((("赤", "M"), ("紺", "L")), start=1)
        ],
    }


def mapped_item() -> dict:
    return map_product_to_shijiu(rich_product(), CATEGORY, excluded_product_numbers=exclusions())


def uploads(item: dict) -> dict:
    return {
        row["upload_reference"]: {
            "status": "UPLOADED",
            "target_url": f"https://cos.example.com/{row['order']:03d}.jpg",
        }
        for row in item["image_upload_plan"]
    }


def mapping_state(item: dict) -> dict:
    return {
        "schema_version": 1,
        "source": "MIKIHOUSE",
        "target": "SHIJIU",
        "identity_contract": {
            "product_name_matching": "forbidden",
            "target_variant_identity": "shijiu_product_id + exact backend_sku_code",
        },
        "products": {
            item["product_number"]: {
                "source": "MIKIHOUSE",
                "source_product_id": item["source_product_id"],
                "product_number": item["product_number"],
                "target_category_id": 294884,
                "shijiu_product_id": None,
                "variants": {
                    row["source_variant_sku"]: {
                        "source": "MIKIHOUSE",
                        "source_variant_id": row["source_variant_id"],
                        "source_variant_sku": row["source_variant_sku"],
                        "backend_sku_code": row["backend_sku_code"],
                        "shijiu_sku_id": None,
                    }
                    for row in item["source_variants"]
                },
            }
        },
    }


def test_stage_plan_chunks_broadcast_and_details_without_exceeding_eight() -> None:
    item = mapped_item()
    plan = staged.stage_plan(item)
    assert plan[0]["operation"] == "CREATE"
    assert plan[0]["broadcast_count"] == 4
    assert plan[0]["detail_pic_count"] == 0
    assert plan[-1]["operation"] == "UPDATE_DETAIL_PICS"
    assert all(len(row["new_references"]) <= 8 for row in plan[1:])
    assert [row["broadcast_count"] for row in plan if row["operation"] == "UPDATE_BROADCAST"] == [12, 20, 27]
    assert [row["detail_pic_count"] for row in plan if row["operation"] == "UPDATE_DETAIL_PICS"] == [8, 16, 24, 26]


def test_stage_payload_is_full_canonical_and_details_images_stay_separate() -> None:
    item = mapped_item()
    plan = staged.stage_plan(item)
    resolved = uploads(item)
    create = staged.build_stage_payload(item, plan[0], resolved)
    assert len(create["sku_info"]) == 2
    assert len(create["spec_name"]) == 2
    assert len(create["broadcast"].split(",")) == 4
    assert create["good_detail_pics"] == ""
    assert "http" not in create["good_details"]
    final = staged.build_stage_payload(item, plan[-1], resolved, product_id="12345")
    validate_canonical_update_payload(final)
    assert final["id"] == 12345
    assert "cdn.shopify.com" not in json.dumps(final)
    assert "https://cos.example.com" not in final["good_details"]
    assert "<img" not in final["good_details"]
    assert len(final["good_details"]) <= 1024
    assert "https://cos.example.com" in final["good_detail_pics"]


def test_update_validator_rejects_patch_shape_and_non_terminal_id() -> None:
    item = mapped_item()
    payload = staged.build_stage_payload(item, staged.stage_plan(item)[0], uploads(item))
    with pytest.raises(ContractMismatchError):
        validate_canonical_update_payload({"id": 1, **payload})
    with pytest.raises(ContractMismatchError):
        validate_canonical_update_payload({"id": 1, "broadcast": "x"})


def test_official_detail_cdn_is_known_but_never_allowed_as_formal_hotlink() -> None:
    assert "img.mksk.me" in OFFICIAL_MIKIHOUSE_IMAGE_HOST_SUFFIXES
    item = mapped_item()
    item["shijiu_payload_preview"]["master_graph"] = "https://img.mksk.me/source.jpg"
    with pytest.raises(staged.LiveImportError, match="hotlink"):
        staged.build_stage_payload(item, staged.stage_plan(item)[0], uploads(item))


class FakeTarget:
    payload: dict | None = None


class FakeWriteClient:
    def __init__(self, target: FakeTarget) -> None:
        self.target = target
        self.requests: list[dict] = []

    def categories(self):
        self.requests.append({"path": "/shopapi/Goodtype/typeindex", "semantic_operation": "read"})
        return {"code": 1, "data": [{
            "id": 288338,
            "type_name": "母婴用品",
            "pid": 0,
            "children": [{"id": 294884, "type_name": "MikiHouse", "pid": 288338}],
        }]}

    def upload_image(self, source_url: str, *, confirmation: str):
        assert confirmation == staged.WRITE_CONFIRMATION
        self.requests.append({"path": "/v1/cos/upload", "semantic_operation": "write"})
        url = "https://cos.example.com/" + source_url.rsplit("/", 1)[-1]
        return url, {"code": 1, "data": {"url": url}}

    def create_product_native(self, payload: dict, *, confirmation: str):
        self.requests.append({
            "path": "/shopapi/Goods/newAddGood",
            "semantic_operation": "write",
            "operation": "native staged create",
        })
        self.target.payload = copy.deepcopy(payload)
        return {"code": 200, "msg": "success", "data": []}

    def update_product_native(self, payload: dict, *, confirmation: str):
        self.requests.append({
            "path": "/shopapi/Goods/newAddGood",
            "semantic_operation": "write",
            "operation": "native staged update",
        })
        self.target.payload = {key: value for key, value in payload.items() if key != "id"}
        return {"code": 200, "msg": "success", "data": []}


class FailingUpdateClient(FakeWriteClient):
    def __init__(self, target: FakeTarget) -> None:
        super().__init__(target)
        self.update_calls = 0

    def update_product_native(self, payload: dict, *, confirmation: str):
        self.update_calls += 1
        self.requests.append({
            "path": "/shopapi/Goods/newAddGood",
            "semantic_operation": "write",
            "operation": "native staged update",
        })
        raise TimeoutError("fixture mutation result unknown")


class FakeUiClient:
    def __init__(self, target: FakeTarget) -> None:
        self.target = target
        self.requests: list[dict] = []

    def exact_name_candidates(self, name: str):
        self.requests.append({"path": "/shopapi/Goods/index", "semantic_operation": "read"})
        rows = [] if self.target.payload is None else [{
            "id": "99001", "good_name": self.target.payload["good_name"], "state": 1, "is_shelf": 0,
        }]
        return rows, {
            "primary_identity_path": "UI exact name",
            "candidate_product_ids": [row["id"] for row in rows],
            "queries": [],
        }

    def product_detail(self, product_id: str):
        self.requests.append({"path": "/shopapi/goods/getFormatInfo", "semantic_operation": "read"})
        return {"code": 200, "msg": "success", "data": {"id": product_id, **copy.deepcopy(self.target.payload)}}


def test_runner_advances_exactly_one_save_per_invocation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(staged.time, "sleep", lambda _: None)
    monkeypatch.setattr(staged, "PROTECTED_FROZEN_FILES", ("frozen.json",))
    (tmp_path / "frozen.json").write_text("{}")
    item = mapped_item()
    selection = {
        "mode": staged.MODE,
        "fixed_target_category_id": 294884,
        "product": {"product_number": item["product_number"]},
        "stages": staged.stage_plan(item),
        "protected_frozen_evidence": {
            "frozen.json": hashlib.sha256(b"{}").hexdigest(),
        },
    }
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(json.dumps(mapping_state(item)), encoding="utf-8")
    target = FakeTarget()
    paths = {
        "checkpoint_path": tmp_path / "checkpoint.json",
        "mapping_path": mapping_path,
        "report_path": tmp_path / "report.json",
        "readbacks_path": tmp_path / "readbacks.json",
    }

    first_client = FakeWriteClient(target)
    first = staged.StagedMediaRunner(
        first_client, FakeUiClient(target), item, exclusions(), CATEGORY, selection,
        root=tmp_path, confirmation=staged.WRITE_CONFIRMATION, **paths,
    )
    first_report = first.run_next_step()
    assert first_report["status"] == "READY_FOR_NEXT_STAGE"
    assert first_report["request_counts"]["create"] == 1
    assert first_report["request_counts"]["update"] == 0
    assert first.checkpoint["stage_cursor"] == 1

    second_client = FakeWriteClient(target)
    second = staged.StagedMediaRunner(
        second_client, FakeUiClient(target), item, exclusions(), CATEGORY, selection,
        root=tmp_path, confirmation=staged.WRITE_CONFIRMATION, **paths,
    )
    second_report = second.run_next_step()
    assert second_report["request_counts"]["create"] == 1
    assert second_report["request_counts"]["update"] == 1
    assert second.checkpoint["stage_cursor"] == 2
    assert second.checkpoint["stages"][1]["pre_update_getFormatInfo_snapshot"]


def test_runner_never_retries_a_mutation_and_records_sent_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(staged.time, "sleep", lambda _: None)
    monkeypatch.setattr(staged, "PROTECTED_FROZEN_FILES", ("frozen.json",))
    (tmp_path / "frozen.json").write_text("{}")
    item = mapped_item()
    selection = {
        "mode": staged.MODE,
        "fixed_target_category_id": 294884,
        "product": {"product_number": item["product_number"]},
        "stages": staged.stage_plan(item),
        "protected_frozen_evidence": {
            "frozen.json": hashlib.sha256(b"{}").hexdigest(),
        },
    }
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(json.dumps(mapping_state(item)), encoding="utf-8")
    target = FakeTarget()
    paths = {
        "checkpoint_path": tmp_path / "checkpoint.json",
        "mapping_path": mapping_path,
        "report_path": tmp_path / "report.json",
        "readbacks_path": tmp_path / "readbacks.json",
    }
    staged.StagedMediaRunner(
        FakeWriteClient(target), FakeUiClient(target), item, exclusions(), CATEGORY, selection,
        root=tmp_path, confirmation=staged.WRITE_CONFIRMATION, **paths,
    ).run_next_step()
    failing = FailingUpdateClient(target)
    runner = staged.StagedMediaRunner(
        failing, FakeUiClient(target), item, exclusions(), CATEGORY, selection,
        root=tmp_path, confirmation=staged.WRITE_CONFIRMATION, **paths,
    )
    with pytest.raises(TimeoutError, match="result unknown"):
        runner.run_next_step()
    assert failing.update_calls == 1
    assert runner.checkpoint["status"] == "FROZEN_ON_FIRST_ANOMALY"
    assert runner.checkpoint["first_failed_state"]["mutation_request_sent"] is True


def test_pdf_special_is_rejected_before_any_target_request(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(staged, "PROTECTED_FROZEN_FILES", ("frozen.json",))
    (tmp_path / "frozen.json").write_text("{}")
    item = mapped_item()
    special = exclusions()
    special.remove(next(iter(special)))
    special.add(item["product_number"])
    selection = {
        "mode": staged.MODE,
        "product": {"product_number": item["product_number"]},
        "stages": staged.stage_plan(item),
        "protected_frozen_evidence": {
            "frozen.json": hashlib.sha256(b"{}").hexdigest(),
        },
    }
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(json.dumps(mapping_state(item)), encoding="utf-8")
    target = FakeTarget()
    client = FakeWriteClient(target)
    runner = staged.StagedMediaRunner(
        client,
        FakeUiClient(target),
        item,
        special,
        CATEGORY,
        selection,
        root=tmp_path,
        checkpoint_path=tmp_path / "checkpoint.json",
        mapping_path=mapping_path,
        report_path=tmp_path / "report.json",
        readbacks_path=tmp_path / "readbacks.json",
        confirmation=staged.WRITE_CONFIRMATION,
    )
    with pytest.raises(staged.LiveImportError, match="PDF_SPECIAL_LIST"):
        runner.run_next_step()
    assert client.requests == []


def test_checked_in_staged_validation_is_frozen_after_local_pre_request_block() -> None:
    root = Path(__file__).resolve().parents[1]
    selection = json.loads((root / "config/shijiu_staged_rich_media_single.json").read_text())
    checkpoint = json.loads(
        (root / "state/shijiu_staged_rich_media_single_checkpoint.json").read_text()
    )
    report = json.loads(
        (root / "deliverables/shijiu_import/staged_rich_media_validation_report.json").read_text()
    )
    conclusion = json.loads(
        (root / "deliverables/shijiu_import/staged_rich_media_capacity_conclusion.json").read_text()
    )
    mapping = json.loads((root / "state/shijiu_mappings.json").read_text())
    assert selection["product"]["product_number"] == "10-8375-578"
    assert selection["product"]["variant_count"] == 2
    assert selection["product"]["official_image_count"] == 27
    assert checkpoint["status"] == "FROZEN_ON_FIRST_ANOMALY"
    assert checkpoint["stage_cursor"] == 3
    assert [row["state"] for row in checkpoint["stages"][:4]] == [
        "VERIFIED", "VERIFIED", "VERIFIED", "FROZEN_ON_ANOMALY"
    ]
    assert [row["attempts"] for row in checkpoint["stages"][:4]] == [1, 1, 1, 0]
    failed = checkpoint["stages"][3]
    assert failed["post_failure_readonly_confirmation"]["verified_broadcast_count"] == 20
    assert failed["post_failure_readonly_confirmation"]["target_mutations"] == 0
    assert checkpoint["first_failed_state"]["mutation_request_sent"] is False
    assert checkpoint["first_failed_state"]["target_upload_request_sent"] is False
    assert report["request_counts"] == {
        "read": 206, "write": 24, "image_upload": 21, "create": 1, "update": 2,
    }
    assert report["mapping_persisted"] is True
    assert conclusion["observed_stable_success"]["maximum_ordered_broadcast_url_count"] == 20
    assert conclusion["first_target_rejected_state"] is None
    assert conclusion["server_hard_limit_proven"] is False
    plan = json.loads(
        (root / "deliverables/shijiu_import/staged_rich_media_update_plan.json").read_text()
    )
    assert plan["further_execution_authorized"] is False
    mapped = mapping["products"]["10-8375-578"]
    assert mapped["shijiu_product_id"] == "9358250"
    assert all(row["shijiu_sku_id"] is None for row in mapped["variants"].values())
    assert all(
        selection["protected_frozen_evidence"][relative]
        == hashlib.sha256((root / relative).read_bytes()).hexdigest()
        for relative in staged.PROTECTED_FROZEN_FILES
    )
    serialized = json.dumps(
        {"selection": selection, "report": report, "conclusion": conclusion},
        ensure_ascii=False,
    ).casefold()
    assert not any(f'"{key}":' in serialized for key in ("token", "secret", "cookie", "authorization"))
