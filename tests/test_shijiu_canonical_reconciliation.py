from __future__ import annotations

import copy
import json
from pathlib import Path

from mikihouse_luyao.shijiu_canonical_reconciliation import (
    EXPECTED_BACKEND_SKU_CODE,
    RECONCILIATION_PRODUCT_NUMBER,
    reconcile_historical_create_read_only,
)
from mikihouse_luyao.shijiu_import import (
    load_mapping_state,
    map_product_to_shijiu,
    reconcile_mapping_state,
    write_json_atomic,
)
from mikihouse_luyao.shijiu_live_import import _resolve_payload
from mikihouse_luyao.shijiu_minimal_probe import TARGET_CATEGORY


def source_product() -> dict:
    image = {"url": "https://cdn.shopify.com/reconcile.jpg", "width": 700, "height": 700}
    return {
        "product_number": RECONCILIATION_PRODUCT_NUMBER,
        "handle": RECONCILIATION_PRODUCT_NUMBER,
        "name": "ヘアゴム（2個セット）",
        "brand": "ミキハウス フォーマル",
        "product_type": "雑貨",
        "category": {"name": "Goods"},
        "tags": [],
        "description": "公式説明",
        "main_image": image,
        "ordered_images": [{"order": 1, "role": "main", "image": image}],
        "product_url": f"https://www.mikihouse.co.jp/products/{RECONCILIATION_PRODUCT_NUMBER}",
        "active": True,
        "variants": [{
            "sku": "36-2001-57200039999",
            "active": True,
            "available_for_sale": True,
            "selected_options": [
                {"name": "カラー", "value": "紺"},
                {"name": "サイズ", "value": "---"},
            ],
            "color": "紺",
            "size": "---",
            "tax_included_price_jpy": 2200,
            "mini_program_price_jpy": 1430,
            "resolved_image": image,
        }],
    }


class ReadOnlyNameClient:
    def __init__(self, payload: dict, *, wrong_price: bool = False) -> None:
        self.payload = payload
        self.wrong_price = wrong_price
        self.requests: list[dict] = []

    def _record(self, path: str) -> None:
        self.requests.append({
            "sequence": len(self.requests) + 1,
            "path": path,
            "semantic_operation": "read",
        })

    def search_products(self, sku_code="", **kwargs):
        self._record("/shopapi/Goods/index")
        row = {
            "id": "99123",
            "good_name": self.payload["good_name"],
            "state": 1,
            "is_shelf": 0,
        }
        if kwargs.get("good_name") == self.payload["good_name"]:
            return {"code": 1, "count": 1, "data": [row]}
        if kwargs.get("good_name") == "" and not sku_code:
            return {"code": 1, "count": 1, "data": [row]}
        return {"code": 1, "count": 0, "data": []}

    def product_detail(self, product_id):
        self._record("/shopapi/goods/getFormatInfo")
        sku = self.payload["sku_info"][0]
        return {"code": 1, "data": {
            "id": product_id,
            "good_name": self.payload["good_name"],
            "good_type": 294884,
            "state": 1,
            "is_shelf": 0,
            "master_graph": self.payload["master_graph"],
            "broadcast": self.payload["broadcast"],
            "good_details": self.payload["good_details"],
            "sku_info": [{
                "id": "generic-nested-id-must-not-be-mapped",
                "sku_code": sku["sku_code"],
                "price": "9999.00" if self.wrong_price else sku["sku_price"],
                "stock": sku["sku_stock"],
                "spec_son_name": ["紺", "---"],
                "sku_thumbnail": sku["sku_thumbnail"],
            }],
        }}


def setup_files(tmp_path: Path):
    special = {f"99-{index:04d}-999" for index in range(351)}
    product = source_product()
    item = map_product_to_shijiu(product, TARGET_CATEGORY, excluded_product_numbers=special)
    uploads = {
        item["image_upload_plan"][0]["upload_reference"]: {
            "status": "UPLOADED",
            "target_url": "https://cdn0.19mini.com/shop/reconcile.jpg",
        }
    }
    payload = _resolve_payload(item, uploads)
    checkpoint = {
        "source": "MIKIHOUSE",
        "target": "SHIJIU",
        "status": "STOPPED_ON_FIRST_ERROR",
        "scope": {"product_numbers": [RECONCILIATION_PRODUCT_NUMBER]},
        "create_attempts": 1,
        "create_response": {"code": 200, "data": [], "msg": "success"},
        "image_uploads": uploads,
        "request_ledger": [],
        "error": {"type": "ContractMismatchError", "message": "old good_code false negative"},
        "shijiu_product_id": None,
        "mapping_persisted": False,
    }
    checkpoint_path = tmp_path / "checkpoint.json"
    mapping_path = tmp_path / "mapping.json"
    validation_path = tmp_path / "validation.json"
    candidate_path = tmp_path / "candidate.json"
    report_path = tmp_path / "reconciliation.json"
    write_json_atomic(checkpoint_path, checkpoint)
    mapping = reconcile_mapping_state(
        load_mapping_state(tmp_path / "missing.json"), [product], TARGET_CATEGORY
    )
    write_json_atomic(mapping_path, mapping)
    write_json_atomic(validation_path, {"status": "STOPPED_ON_FIRST_ERROR"})
    write_json_atomic(candidate_path, {"result": "STOPPED_ON_FIRST_ERROR"})
    return (
        item,
        payload,
        checkpoint,
        special,
        checkpoint_path,
        mapping_path,
        validation_path,
        candidate_path,
        report_path,
    )


def test_exact_name_primary_path_binds_when_good_code_search_returns_zero(tmp_path: Path) -> None:
    args = setup_files(tmp_path)
    item, payload, checkpoint, special, *paths = args
    client = ReadOnlyNameClient(payload)
    report = reconcile_historical_create_read_only(
        client,
        item,
        payload,
        copy.deepcopy(checkpoint),
        special,
        checkpoint_path=paths[0],
        mapping_path=paths[1],
        validation_report_path=paths[2],
        candidate_report_path=paths[3],
        reconciliation_report_path=paths[4],
    )
    assert report["status"] == "RECONCILED_READBACK_VERIFIED"
    assert report["shijiu_product_id"] == "99123"
    assert report["auxiliary_good_code_product_ids"] == []
    assert report["target_mutations_this_run"] == 0
    assert all(row["semantic_operation"] == "read" for row in client.requests)
    mapping = json.loads(paths[1].read_text())
    mapped = mapping["products"][RECONCILIATION_PRODUCT_NUMBER]
    assert mapped["shijiu_product_id"] == "99123"
    variant = mapped["variants"]["36-2001-57200039999"]
    assert variant["backend_sku_code"] == EXPECTED_BACKEND_SKU_CODE
    assert variant["shijiu_sku_id"] is None


def test_mismatched_price_keeps_mapping_unbound_after_full_category_scan(tmp_path: Path) -> None:
    args = setup_files(tmp_path)
    item, payload, checkpoint, special, *paths = args
    client = ReadOnlyNameClient(payload, wrong_price=True)
    report = reconcile_historical_create_read_only(
        client,
        item,
        payload,
        copy.deepcopy(checkpoint),
        special,
        checkpoint_path=paths[0],
        mapping_path=paths[1],
        validation_report_path=paths[2],
        candidate_report_path=paths[3],
        reconciliation_report_path=paths[4],
    )
    assert report["status"] == "RECONCILIATION_NO_UNIQUE_STRONG_EVIDENCE"
    assert report["full_category_scan_used"] is True
    assert report["mapping_persisted"] is False
    mapping = json.loads(paths[1].read_text())
    assert mapping["products"][RECONCILIATION_PRODUCT_NUMBER]["shijiu_product_id"] is None
    assert all(row["semantic_operation"] == "read" for row in client.requests)
