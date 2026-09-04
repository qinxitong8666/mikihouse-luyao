from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from mikihouse_luyao.shijiu_recovery import (
    RECOVERY_CONFIRMATION,
    RECOVERY_PRODUCT_NUMBER,
    FirstProductRecoveryRunner,
    load_recovery_inputs,
)


ROOT = Path(__file__).resolve().parents[1]


def inputs():
    return load_recovery_inputs(
        ROOT / "deliverables/shijiu_import/payload_previews.json",
        ROOT / "state/shijiu_first_live_batch_checkpoint.json",
        ROOT / "state/shijiu_mappings.json",
        ROOT / "special_skus_2026aw.csv",
        ROOT / "config/shijiu_native_create_contract.json",
    )


def minimal_mapping(item: dict) -> dict:
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
            RECOVERY_PRODUCT_NUMBER: {
                "source": "MIKIHOUSE",
                "source_product_id": item["source_product_id"],
                "product_number": RECOVERY_PRODUCT_NUMBER,
                "source_present": True,
                "target_category_id": 294884,
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
        },
    }


class RecoveryClient:
    def __init__(self, payload: dict, *, baseline_count: int = 286, sku_id: str = "88001"):
        self.payload = payload
        self.baseline_count = baseline_count
        self.sku_id = sku_id
        self.created = False
        self.requests = []

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

    def _new_row(self):
        return {
            "id": "99001",
            "good_name": self.payload["good_name"],
            "good_type": 294884,
            "state": 1,
            "is_shelf": 0,
            "master_graph": self.payload["master_graph"],
        }

    def search_products(
        self,
        sku_code="",
        *,
        good_name="",
        status="",
        push="2",
        good_type=294884,
        page=1,
        page_size=20,
        **filters,
    ):
        self._record("/shopapi/Goods/index", "read")
        if sku_code or good_name:
            rows = [self._new_row()] if self.created else []
            return {"code": 1, "count": len(rows), "data": rows}
        count = self.baseline_count + (1 if self.created else 0)
        legacy = [
            {"id": str(800000 + index), "good_name": f"legacy-{index}"}
            for index in range(self.baseline_count)
        ]
        rows = legacy + ([self._new_row()] if self.created else [])
        start = (page - 1) * page_size
        return {"code": 1, "count": count, "data": rows[start:start + page_size]}

    def create_product(self, payload, *, confirmation):
        assert confirmation == RECOVERY_CONFIRMATION
        assert payload["state"] == "1" and payload["is_shelf"] == 0
        self._record("/shopapi/Goods/newAddGood", "write")
        self.created = True
        return {"code": 200, "msg": "success", "data": []}

    def product_detail(self, product_id):
        self._record("/shopapi/goods/getFormatInfo", "read")
        source = self.payload["sku_info"][0]
        return {
            "code": 1,
            "msg": "查询成功",
            "data": {
                "id": str(product_id),
                "good_name": self.payload["good_name"],
                "good_type": 294884,
                "state": 1,
                "is_shelf": 0,
                "master_graph": self.payload["master_graph"],
                "broadcast": self.payload["broadcast"],
                "good_detail_pics": self.payload["good_detail_pics"],
                "good_details": self.payload["good_details"],
                "sku_info": [{
                    "sku_id": self.sku_id,
                    "sku_code": source["sku_code"],
                    "price": source["sku_price"],
                    "stock": int(float(source["sku_stock"])),
                    "spec_son_name": source["spec_name"].split(","),
                    "sku_thumbnail": source["sku_thumbnail"],
                }],
            },
        }


class UnobservableRecoveryClient(RecoveryClient):
    def create_product(self, payload, *, confirmation):
        assert confirmation == RECOVERY_CONFIRMATION
        assert payload["state"] == "1" and payload["is_shelf"] == 0
        self._record("/shopapi/Goods/newAddGood", "write")
        return {"code": 200, "msg": "success", "data": []}


def make_runner(tmp_path: Path, *, baseline_count: int = 286):
    item, original_record, payload, _, contract = inputs()
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(json.dumps(minimal_mapping(item)), encoding="utf-8")
    client = RecoveryClient(payload, baseline_count=baseline_count)
    runner = FirstProductRecoveryRunner(
        client,
        item,
        original_record,
        payload,
        contract,
        tmp_path / "checkpoint.json",
        mapping_path,
        tmp_path / "report.json",
        tmp_path / "residual.json",
        tmp_path / "readback.json",
        confirmation=RECOVERY_CONFIRMATION,
    )
    return runner, client, mapping_path


def test_native_recovery_contract_changes_only_state_semantics() -> None:
    item, record, payload, special, contract = inputs()
    assert item["product_number"] == RECOVERY_PRODUCT_NUMBER
    assert RECOVERY_PRODUCT_NUMBER not in special and len(special) == 351
    assert payload["state"] == contract["state"] == "1"
    assert payload["is_shelf"] == contract["is_shelf"] == 0
    assert contract["reference_commit"] == "a36c5eab40bf419562ba03d15c090151698d582a"
    assert contract["reference_file_sha256"] == (
        "1183564685c35f1f79b684077fbb1b1f7ed6bd2b9a46d227edb18ade94c9d016"
    )
    assert list(payload) == contract["product_fields"]
    assert list(payload["sku_info"][0]) == contract["sku_fields"]
    assert len(record["image_uploads"]) == 12


def test_controlled_recovery_reuses_images_and_writes_exactly_once(tmp_path: Path) -> None:
    runner, client, mapping_path = make_runner(tmp_path)
    report = runner.run()
    assert report["state"] == "RECOVERY_READBACK_VERIFIED"
    assert report["images"] == {"prior_cos_images_reused": 12, "new_upload_requests": 0}
    assert report["requests"]["product_create"] == 1
    assert report["requests"]["image_upload"] == 0
    assert report["subsequent_19_products_processed"] == 0
    assert report["legacy_products_modified"] == 0
    mapping = json.loads(mapping_path.read_text())
    row = mapping["products"][RECOVERY_PRODUCT_NUMBER]
    assert row["shijiu_product_id"] == "99001"
    assert next(iter(row["variants"].values()))["shijiu_sku_id"] == "88001"
    before = len(client.requests)
    with pytest.raises(Exception, match="terminal"):
        runner.run()
    assert len(client.requests) == before


def test_residual_scan_can_be_completed_before_the_single_write(tmp_path: Path) -> None:
    runner, client, _ = make_runner(tmp_path)
    readonly = runner.run_residual_only()
    assert readonly["state"] == "RESIDUAL_ABSENCE_PROVEN"
    assert readonly["residual_absence_proven"] is True
    assert readonly["requests"]["write"] == 0
    assert readonly["requests"]["image_upload"] == 0
    completed = runner.run()
    assert completed["requests"]["product_create"] == 1


def test_recovery_refuses_create_if_legacy_baseline_count_changed(tmp_path: Path) -> None:
    runner, client, mapping_path = make_runner(tmp_path, baseline_count=285)
    with pytest.raises(Exception, match="residual absence was not proven"):
        runner.run()
    assert all(row["path"] != "/shopapi/Goods/newAddGood" for row in client.requests)
    assert all(row["path"] != "/v1/cos/upload" for row in client.requests)
    mapping = json.loads(mapping_path.read_text())
    assert mapping["products"][RECOVERY_PRODUCT_NUMBER]["shijiu_product_id"] is None


def test_post_recovery_forensics_is_read_only_and_requires_consumed_budget(
    tmp_path: Path,
) -> None:
    runner, client, _ = make_runner(tmp_path)
    with pytest.raises(Exception, match="stopped checkpoint"):
        runner.run_post_recovery_forensics(tmp_path / "forensics.json")
    runner.checkpoint["state"] = "STOPPED_ON_RECOVERY_ERROR"
    runner.checkpoint["recovery_create_attempts"] = 1
    client.created = False
    result = runner.run_post_recovery_forensics(tmp_path / "forensics.json")
    assert result["residual_absence_still_proven"] is True
    assert result["requests"]["read"] > 0
    assert result["requests"]["write"] == 0
    assert result["requests"]["image_upload"] == 0
    assert result["requests"]["product_create"] == 0


def test_unobservable_success_response_stops_without_mapping_or_second_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item, original_record, payload, _, contract = inputs()
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(json.dumps(minimal_mapping(item)), encoding="utf-8")
    client = UnobservableRecoveryClient(payload)
    runner = FirstProductRecoveryRunner(
        client,
        item,
        original_record,
        payload,
        contract,
        tmp_path / "checkpoint.json",
        mapping_path,
        tmp_path / "report.json",
        tmp_path / "residual.json",
        tmp_path / "readback.json",
        confirmation=RECOVERY_CONFIRMATION,
    )
    monkeypatch.setattr("mikihouse_luyao.shijiu_recovery.time.sleep", lambda _: None)
    with pytest.raises(Exception, match=r"exact verified IDs=\[\]"):
        runner.run()
    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text())
    assert checkpoint["state"] == "STOPPED_ON_RECOVERY_ERROR"
    assert checkpoint["recovery_create_attempts"] == 1
    assert checkpoint["shijiu_product_id"] is None
    assert sum(
        row["path"] == "/shopapi/Goods/newAddGood" for row in client.requests
    ) == 1
    assert all(row["path"] != "/v1/cos/upload" for row in client.requests)
    mapping = json.loads(mapping_path.read_text())
    assert mapping["products"][RECOVERY_PRODUCT_NUMBER]["shijiu_product_id"] is None
    before = len(client.requests)
    with pytest.raises(Exception, match="terminal"):
        runner.run()
    assert len(client.requests) == before
