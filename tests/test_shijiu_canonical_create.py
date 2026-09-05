from __future__ import annotations

import copy
import csv
import json
from pathlib import Path

from mikihouse_luyao.shijiu_canonical_create import (
    CANONICAL_CREATE_CONFIRMATION,
    CanonicalCreateRunner,
)
from mikihouse_luyao.shijiu_import import (
    load_mapping_state,
    map_product_to_shijiu,
    reconcile_mapping_state,
    write_json_atomic,
)
from mikihouse_luyao.shijiu_live_import import validate_canonical_create_payload
from mikihouse_luyao.shijiu_minimal_probe import TARGET_CATEGORY


ROOT = Path(__file__).resolve().parents[1]


def source_product() -> dict:
    image = {"url": "https://cdn.shopify.com/test.jpg", "width": 1200, "height": 1200}
    return {
        "product_number": "36-9999-999",
        "handle": "36-9999-999",
        "name": "検証商品",
        "brand": "ミキハウス",
        "product_type": "雑貨",
        "category": {"name": "Goods"},
        "tags": [],
        "description": "公式説明",
        "main_image": image,
        "ordered_images": [{"order": 1, "role": "main", "image": image}],
        "product_url": "https://www.mikihouse.co.jp/products/36-9999-999",
        "active": True,
        "variants": [{
            "sku": "36-9999-99900039999",
            "active": True,
            "available_for_sale": True,
            "selected_options": [{"name": "サイズ", "value": "フリー"}],
            "color": "",
            "size": "フリー",
            "tax_included_price_jpy": 2200,
            "mini_program_price_jpy": 1430,
            "resolved_image": image,
        }],
    }


class FakeCanonicalClient:
    def __init__(self) -> None:
        self.created = False
        self.payload = None
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

    def search_products(self, sku_code="", **kwargs):
        self._record("/shopapi/Goods/index", "read")
        exact_name = kwargs.get("good_name") == (self.payload or {}).get("good_name")
        rows = ([{
                "id": "99077",
                "good_name": self.payload["good_name"],
                "state": 1,
                "is_shelf": 0,
            }] if self.created and exact_name else [])
        return {
            "code": 1,
            "data": {"count": len(rows), "list": rows},
        }

    def upload_image(self, source_url, *, confirmation):
        assert confirmation == CANONICAL_CREATE_CONFIRMATION
        self._record("/v1/cos/upload", "write")
        return "https://cos.example.com/canonical.jpg", {"code": 1}

    def create_product(self, payload, *, confirmation):
        assert confirmation == CANONICAL_CREATE_CONFIRMATION
        validate_canonical_create_payload(payload)
        self._record("/shopapi/Goods/newAddGood", "write")
        self.created = True
        self.payload = copy.deepcopy(payload)
        return {"code": 200, "msg": "success", "data": []}

    def product_detail(self, product_id):
        self._record("/shopapi/goods/getFormatInfo", "read")
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
                "sku_code": sku["sku_code"],
                "price": sku["sku_price"],
                "stock": sku["sku_stock"],
                "spec_son_name": ["フリー"],
                "sku_thumbnail": sku["sku_thumbnail"],
            }],
        }}


def test_single_canonical_create_persists_product_plus_exact_code_identity(tmp_path) -> None:
    special = {f"99-{index:04d}-999" for index in range(351)}
    product = source_product()
    item = map_product_to_shijiu(product, TARGET_CATEGORY, excluded_product_numbers=special)
    mapping = reconcile_mapping_state(
        load_mapping_state(tmp_path / "missing.json"), [product], TARGET_CATEGORY
    )
    mapping_path = tmp_path / "mapping.json"
    write_json_atomic(mapping_path, mapping)
    client = FakeCanonicalClient()
    runner = CanonicalCreateRunner(
        client,
        item,
        special,
        {"selected_product_number": item["product_number"]},
        {"private_evidence_sha256": "test", "persistence_verified": True},
        tmp_path / "checkpoint.json",
        mapping_path,
        tmp_path / "report.json",
        confirmation=CANONICAL_CREATE_CONFIRMATION,
    )
    report = runner.run()
    assert report["status"] == "COMPLETED"
    assert report["create_request_count"] == 1
    assert report["legacy_reference_touched"] is False
    state = json.loads(mapping_path.read_text())
    row = state["products"][item["product_number"]]
    variant = next(iter(row["variants"].values()))
    assert row["shijiu_product_id"] == "99077"
    assert variant["target_product_id"] == "99077"
    assert variant["backend_sku_code_verified"] is True
    assert variant["shijiu_sku_id"] is None
    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text())
    assert checkpoint["readback_discovery"]["candidate_product_ids"] == ["99077"]
    assert checkpoint["readback_discovery"]["auxiliary_good_code_product_ids"] == []
    assert checkpoint["readback_discovery"]["good_code_role"] == "auxiliary_only_never_binding"
    before = len(client.requests)
    runner.run()
    assert len(client.requests) == before


def test_checked_in_ui_context_reconciliation_binds_only_verified_historical_create() -> None:
    report = json.loads(
        (ROOT / "deliverables/shijiu_import/canonical_create_validation_report.json").read_text()
    )
    candidate = json.loads(
        (ROOT / "deliverables/shijiu_import/canonical_create_candidate.json").read_text()
    )
    checkpoint = json.loads(
        (ROOT / "state/shijiu_canonical_create_checkpoint.json").read_text()
    )
    reconciliation = json.loads(
        (ROOT / "deliverables/shijiu_import/canonical_create_reconciliation_report.json").read_text()
    )
    ui_reconciliation = json.loads(
        (ROOT / "deliverables/shijiu_import/canonical_create_ui_context_reconciliation_report.json").read_text()
    )
    mapping = json.loads((ROOT / "state/shijiu_mappings.json").read_text())
    with (ROOT / "special_skus_2026aw.csv").open(newline="", encoding="utf-8-sig") as handle:
        special = {row["product_number"] for row in csv.DictReader(handle)}
    number = report["product_number"]
    assert report["status"] == "RECONCILED_READBACK_VERIFIED_UI_CONTEXT"
    assert report["create_request_count"] == report["create_attempts"] == 1
    assert report["image_upload_count"] == 1
    assert report["exact_backend_sku_match_count"] == 1
    assert report["mapping_persisted"] is True
    assert report["additional_product_create_requests_allowed"] is False
    assert report["legacy_reference_touched"] is False
    assert report["pdf_special_exclusion_count"] == len(special) == 351
    assert number not in special
    assert candidate["write_executed"] is True
    assert candidate["result"] == "RECONCILED_READBACK_VERIFIED_UI_CONTEXT"
    assert candidate["additional_write_executed"] is False
    assert checkpoint["status"] == "RECONCILED_READBACK_VERIFIED_UI_CONTEXT"
    assert checkpoint["scope"]["product_numbers"] == [number]
    assert checkpoint["create_attempts"] == 1
    assert reconciliation["create_requests_this_run"] == 0
    assert reconciliation["image_upload_requests_this_run"] == 0
    assert reconciliation["update_requests_this_run"] == 0
    assert reconciliation["target_mutations_this_run"] == 0
    assert reconciliation["full_category_scan_used"] is True
    assert reconciliation["candidate_product_ids"] == []
    assert reconciliation["verified_product_ids"] == []
    assert reconciliation["auxiliary_good_code_product_ids"] == []
    assert reconciliation["good_code_role"] == "auxiliary_only_never_binding"
    assert reconciliation["sensitive_values_included"] is False
    assert ui_reconciliation["status"] == "RECONCILED_READBACK_VERIFIED_UI_CONTEXT"
    assert ui_reconciliation["candidate_product_ids"] == ["9358233"]
    assert ui_reconciliation["verified_product_ids"] == ["9358233"]
    assert ui_reconciliation["safety"]["read_only_requests"] == 36
    assert ui_reconciliation["safety"]["create_requests"] == 0
    assert ui_reconciliation["safety"]["image_upload_requests"] == 0
    assert ui_reconciliation["safety"]["update_requests"] == 0
    assert ui_reconciliation["sensitive_values_included"] is False
    assert mapping["products"][number]["shijiu_product_id"] == "9358233"
    assert all(
        row["shijiu_sku_id"] is None
        for row in mapping["products"][number]["variants"].values()
    )
