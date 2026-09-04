from __future__ import annotations

import copy
import json
import urllib.error
from pathlib import Path

import pytest

import mikihouse_luyao.shijiu_live_import as live

from mikihouse_luyao.shijiu_import import (
    backend_sku_code,
    content_sha256,
    source_product_id,
    source_variant_id,
)
from mikihouse_luyao.shijiu_live_import import (
    LIVE_WRITE_CONFIRMATION,
    ContractMismatchError,
    FirstLiveBatchRunner,
    ShijiuLiveClient,
    _resolve_payload,
    initial_checkpoint,
    validate_product_readback,
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


def mapped_item(number: str = "20-0001-001", sku: str = "sku-1") -> dict:
    reference = f"MIKIHOUSE:{number}:IMAGE:001"
    placeholder = f"{{{{SHIJIU_COS_URL:{reference}}}}}"
    payload = {
        "good_name": f"商品 {number}",
        "good_type": 294884,
        "state": "1",
        "is_shelf": 0,
        "master_graph": placeholder,
        "broadcast": placeholder,
        "good_detail_pics": placeholder,
        "good_details": f'<section><img src="{placeholder}"></section>',
        "spec_name": [{"spec_name": "颜色", "id": 0, "son_name": []}],
        "sku_info": [{
            "sku_code": backend_sku_code(sku),
            "sku_price": "650.00",
            "sku_stock": "1.00",
            "spec_name": "赤,12.5cm",
            "sku_cost_price": "1000.00",
            "sku_thumbnail": placeholder,
        }],
    }
    return {
        "source": "MIKIHOUSE",
        "source_product_id": source_product_id(number),
        "product_number": number,
        "classification": "footwear",
        "payload_sha256": content_sha256(payload),
        "publish_ready": True,
        "target_category": CATEGORY,
        "source_variants": [{
            "source_variant_id": source_variant_id(number, sku),
            "source_variant_sku": sku,
            "backend_sku_code": backend_sku_code(sku),
        }],
        "image_upload_plan": [{
            "upload_reference": reference,
            "order": 1,
            "role": "main",
            "source_url": f"https://cdn.shopify.com/{number}.jpg",
        }],
        "shijiu_payload_preview": payload,
    }


def uploaded(item: dict) -> dict:
    reference = item["image_upload_plan"][0]["upload_reference"]
    return {
        reference: {
            "status": "UPLOADED",
            "target_url": "https://cos.example.com/miki.jpg",
        }
    }


def detail_for(payload: dict, *, sku_id: str | None = "88001") -> dict:
    sku = payload["sku_info"][0]
    sku_row = {
        "sku_code": sku["sku_code"],
        "price": sku["sku_price"],
        "stock": 1,
        "spec_son_name": ["赤", "12.5cm"],
        "sku_thumbnail": sku["sku_thumbnail"],
    }
    if sku_id is not None:
        sku_row["sku_id"] = sku_id
    return {
        "code": 1,
        "msg": "查询成功",
        "data": {
            "id": "99001",
            "good_name": payload["good_name"],
            "good_type": 294884,
            "state": 1,
            "is_shelf": 0,
            "master_graph": payload["master_graph"],
            "broadcast": payload["broadcast"],
            "good_detail_pics": payload["good_detail_pics"],
            "good_details": payload["good_details"],
            "sku_info": [sku_row],
        },
    }


def mapping_state(items: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "source": "MIKIHOUSE",
        "target": "SHIJIU",
        "identity_contract": {
            "source_product_id": "MIKIHOUSE:<product_number>",
            "source_variant_id": "MIKIHOUSE:<product_number>:<variant SKU>",
            "backend_sku_code": "MIKI-<variant SKU>",
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
                "last_verified_at": None,
                "variants": {
                    variant["source_variant_sku"]: {
                        "source": "MIKIHOUSE",
                        "source_variant_id": variant["source_variant_id"],
                        "source_variant_sku": variant["source_variant_sku"],
                        "backend_sku_code": variant["backend_sku_code"],
                        "source_present": True,
                        "shijiu_sku_id": None,
                        "last_verified_at": None,
                    }
                    for variant in item["source_variants"]
                },
            }
            for item in items
        },
    }


def test_resolved_payload_uses_only_cos_urls_and_is_off_shelf() -> None:
    item = mapped_item()
    payload = _resolve_payload(item, uploaded(item))
    assert payload["state"] == "1"
    assert payload["is_shelf"] == 0
    assert payload["good_type"] == 294884
    assert "cdn.shopify.com" not in json.dumps(payload)
    assert "SHIJIU_COS_URL" not in json.dumps(payload)
    assert payload["master_graph"] == "https://cos.example.com/miki.jpg"


def test_readback_uses_product_id_and_exact_backend_sku_code_with_nullable_sku_id() -> None:
    item = mapped_item()
    payload = _resolve_payload(item, uploaded(item))
    valid = validate_product_readback(
        item,
        payload,
        "99001",
        detail_for(payload),
        list_row={"id": "99001", "state": 1, "is_shelf": 0},
    )
    assert valid["passed"] is True
    assert valid["shijiu_product_id"] == "99001"
    assert valid["skus"][0]["shijiu_sku_id"] == "88001"
    without_sku_id = validate_product_readback(
        item,
        payload,
        "99001",
        detail_for(payload, sku_id=None),
        list_row={"id": "99001", "state": 1, "is_shelf": 0},
    )
    assert without_sku_id["passed"] is True
    assert without_sku_id["skus"][0]["shijiu_sku_id"] is None
    assert without_sku_id["skus"][0]["stable_target_identity"] == {
        "shijiu_product_id": "99001",
        "backend_sku_code": backend_sku_code("sku-1"),
    }


def test_staged_detail_pics_allow_exact_minimal_html_until_final_html_stage() -> None:
    item = mapped_item()
    payload = _resolve_payload(item, uploaded(item))
    payload["good_details"] = "<section><p>minimal text only</p></section>"
    detail = detail_for(payload)
    valid = validate_product_readback(
        item,
        payload,
        "99001",
        detail,
        list_row={"id": "99001", "state": 1, "is_shelf": 0},
        require_exact_good_details=True,
    )
    assert valid["passed"] is True
    assert valid["detail_image_urls"] == ["https://cos.example.com/miki.jpg"]


class _ReadResponse:
    headers: dict[str, str] = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return b'{"code":1,"data":{}}'


def test_live_client_get_format_info_retries_only_the_read(monkeypatch) -> None:
    outcomes = [
        urllib.error.HTTPError("https://example.invalid", 504, "timeout", {}, None),
        _ReadResponse(),
    ]
    sleeps: list[float] = []

    def next_outcome(*_args, **_kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(live.urllib.request, "urlopen", next_outcome)
    monkeypatch.setattr(live.time, "sleep", sleeps.append)
    client = ShijiuLiveClient("token", "secret", timeout=1)
    result = client.product_detail("123")
    assert result["code"] == 1
    assert sleeps == [0.5]
    assert [row["attempt"] for row in client.requests] == [1, 2]
    assert all(row["semantic_operation"] == "read" for row in client.requests)


def test_checkpoint_has_no_legacy_or_cleanup_actions() -> None:
    checkpoint = initial_checkpoint([mapped_item()])
    assert checkpoint["source"] == "MIKIHOUSE"
    assert checkpoint["fixed_target_category_id"] == 294884
    assert checkpoint["legacy_reference_touched"] is False
    assert checkpoint["legacy_cleanup_executed"] is False


class FakeClient:
    def __init__(self, *, sku_id: str | None = "88001") -> None:
        self.sku_id = sku_id
        self.created = False
        self.payload = None
        self.requests = []

    @property
    def write_request_count(self):
        return sum(row["semantic_operation"] == "write" for row in self.requests)

    def _record(self, path: str, semantic: str):
        self.requests.append({"path": path, "semantic_operation": semantic})

    def categories(self):
        self._record("/shopapi/Goodtype/typeindex", "read")
        return {"code": 1, "data": [{
            "id": 288338,
            "type_name": "母婴用品",
            "pid": 0,
            "children": [{"id": 294884, "type_name": "MikiHouse", "pid": 288338}],
        }]}

    def search_products(self, sku_code="", *, status="", page=1, page_size=20, **kwargs):
        self._record("/shopapi/Goods/index", "read")
        exact_name = kwargs.get("good_name") == (self.payload or {}).get("good_name")
        return {
            "code": 1,
            "msg": "查询成功",
            "count": 1 if self.created and exact_name else 0,
            "data": ([{
                "id": "99001",
                "good_name": self.payload["good_name"],
                "state": 1,
                "is_shelf": 0,
            }] if self.created and exact_name else []),
        }

    def upload_image(self, source_url, *, confirmation):
        assert confirmation == LIVE_WRITE_CONFIRMATION
        self._record("/v1/cos/upload", "write")
        return "https://cos.example.com/miki.jpg", {"code": 1, "data": {"url": "https://cos.example.com/miki.jpg"}}

    def create_product(self, payload, *, confirmation):
        assert confirmation == LIVE_WRITE_CONFIRMATION
        assert payload["state"] == "1" and payload["is_shelf"] == 0
        self._record("/shopapi/Goods/newAddGood", "write")
        self.created = True
        self.payload = copy.deepcopy(payload)
        sku = {**payload["sku_info"][0]}
        if self.sku_id is not None:
            sku["sku_id"] = self.sku_id
        return {"code": 1, "msg": "新增成功", "data": {"id": "99001", "sku_info": [sku]}}

    def product_detail(self, product_id):
        self._record("/shopapi/goods/getFormatInfo", "read")
        return detail_for(self.payload, sku_id=self.sku_id)


def run_one(tmp_path: Path, *, sku_id: str | None = "88001"):
    item = mapped_item()
    checkpoint_path = tmp_path / "checkpoint.json"
    mapping_path = tmp_path / "mapping.json"
    report_path = tmp_path / "report.json"
    readback_path = tmp_path / "readbacks.json"
    mapping_path.write_text(json.dumps(mapping_state([item])), encoding="utf-8")
    client = FakeClient(sku_id=sku_id)
    runner = FirstLiveBatchRunner(
        client,
        [item],
        exclusions(),
        CATEGORY,
        checkpoint_path,
        mapping_path,
        report_path,
        readback_path,
        confirmation=LIVE_WRITE_CONFIRMATION,
    )
    return runner, client, mapping_path, checkpoint_path, report_path


def test_runner_persists_verified_mapping_and_is_idempotent(tmp_path: Path) -> None:
    runner, client, mapping_path, checkpoint_path, report_path = run_one(tmp_path)
    report = runner.run()
    mapping = json.loads(mapping_path.read_text())
    assert report["status"] == "COMPLETED"
    assert mapping["products"]["20-0001-001"]["shijiu_product_id"] == "99001"
    assert mapping["products"]["20-0001-001"]["variants"]["sku-1"]["shijiu_sku_id"] == "88001"
    assert client.write_request_count == 2
    record = json.loads(checkpoint_path.read_text())["records"]["20-0001-001"]
    assert record["state"] == "READBACK_VERIFIED"
    assert record["readback_discovery"]["candidate_product_ids"] == ["99001"]
    assert record["readback_discovery"]["auxiliary_good_code_product_ids"] == []
    assert record["readback_discovery"]["good_code_role"] == "auxiliary_only_never_binding"
    # Reusing a completed runner never sends another mutation.
    runner.run()
    assert client.write_request_count == 2
    assert json.loads(report_path.read_text())["legacy_cleanup_executed"] is False


def test_runner_accepts_nullable_sku_id_after_exact_code_readback(tmp_path: Path) -> None:
    runner, client, mapping_path, checkpoint_path, report_path = run_one(
        tmp_path, sku_id=None
    )
    report = runner.run()
    checkpoint = json.loads(checkpoint_path.read_text())
    report = json.loads(report_path.read_text())
    mapping = json.loads(mapping_path.read_text())
    assert checkpoint["status"] == "COMPLETED"
    assert checkpoint["records"]["20-0001-001"]["shijiu_product_id"] == "99001"
    assert mapping["products"]["20-0001-001"]["shijiu_product_id"] == "99001"
    variant = mapping["products"]["20-0001-001"]["variants"]["sku-1"]
    assert variant["shijiu_sku_id"] is None
    assert variant["target_product_id"] == "99001"
    assert variant["backend_sku_code_verified"] is True
    assert report["verified_product_count"] == 1
    assert report["request_counts"]["product_create"] == 1
    assert report["legacy_reference_touched"] is False
    assert client.write_request_count == 2


def test_live_runner_rejects_pdf_special_before_any_target_request(tmp_path: Path) -> None:
    item = mapped_item()
    forbidden = exclusions()
    forbidden.remove("99-0000-999")
    forbidden.add(item["product_number"])
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(json.dumps(mapping_state([item])), encoding="utf-8")
    client = FakeClient()
    runner = FirstLiveBatchRunner(
        client,
        [item],
        forbidden,
        CATEGORY,
        tmp_path / "checkpoint.json",
        mapping_path,
        tmp_path / "report.json",
        tmp_path / "readbacks.json",
        confirmation=LIVE_WRITE_CONFIRMATION,
    )
    with pytest.raises(Exception, match="PDF_SPECIAL_LIST"):
        runner.run()
    assert client.requests == []
