from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from mikihouse_luyao.shijiu_import import (
    backend_sku_code,
    content_sha256,
    map_product_to_shijiu,
    source_product_id,
    source_variant_id,
)
from mikihouse_luyao.shijiu_live_import import ShijiuLiveClient
from mikihouse_luyao.shijiu_minimal_probe import (
    FORBIDDEN_RECOVERY_PRODUCT,
    PROBE_CONFIRMATION,
    TARGET_CATEGORY,
    MinimalCreateProbeRunner,
    build_minimal_payload,
    select_minimal_probe_candidate,
)


ROOT = Path(__file__).resolve().parents[1]


def product(number: str = "17-1366-244", *, image_count: int = 1) -> dict:
    sku = f"{number}00899999"
    images = [
        {
            "order": index,
            "role": "main" if index == 1 else "product_gallery",
            "image": {
                "url": f"https://cdn.shopify.com/{number}-{index}.jpg",
                "width": 700,
                "height": 700,
                "alt_text": "",
            },
            "variant_skus": [sku] if index == 1 else [],
            "colors": ["色なし"] if index == 1 else [],
        }
        for index in range(1, image_count + 1)
    ]
    main = copy.deepcopy(images[0]["image"])
    return {
        "stable_id": number,
        "product_number": number,
        "handle": number,
        "name": f"MIKI商品 {number}",
        "brand": "ミキハウス",
        "product_type": "通常商品",
        "category": None,
        "tags": [],
        "description": "商品説明",
        "description_html": "<p>商品説明</p>",
        "main_image": main,
        "product_images": [copy.deepcopy(row["image"]) for row in images],
        "media": [],
        "detail_images": [],
        "ordered_images": images,
        "color_images": [],
        "product_url": f"https://www.mikihouse.co.jp/products/{number}",
        "active": True,
        "variants": [
            {
                "stable_id": f"{number}::{sku}",
                "sku": sku,
                "active": True,
                "available_for_sale": True,
                "selected_options": [
                    {"name": "カラー", "value": "色なし"},
                    {"name": "サイズ", "value": "---"},
                ],
                "color": "色なし",
                "size": "---",
                "tax_included_price_jpy": 1650,
                "mini_program_price_jpy": 1073,
                "resolved_image": main,
            }
        ],
    }


def mapping(products: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "source": "MIKIHOUSE",
        "target": "SHIJIU",
        "identity_contract": {"product_name_matching": "forbidden"},
        "products": {
            row["product_number"]: {
                "source": "MIKIHOUSE",
                "source_product_id": source_product_id(row["product_number"]),
                "product_number": row["product_number"],
                "shijiu_product_id": None,
                "variants": {
                    row["variants"][0]["sku"]: {
                        "source": "MIKIHOUSE",
                        "source_variant_id": source_variant_id(
                            row["product_number"], row["variants"][0]["sku"]
                        ),
                        "source_variant_sku": row["variants"][0]["sku"],
                        "backend_sku_code": backend_sku_code(
                            row["variants"][0]["sku"]
                        ),
                        "shijiu_sku_id": None,
                    }
                },
            }
            for row in products
        },
    }


def probe_inputs(row: dict) -> dict:
    special = {f"99-{index:04d}-999" for index in range(351)}
    mapped = map_product_to_shijiu(
        row, TARGET_CATEGORY, excluded_product_numbers=special
    )
    native = json.loads(
        (ROOT / "config/shijiu_native_create_shape_fixture.json").read_text()
    )
    minimal = build_minimal_payload(native, mapped)
    return {
        "master_file_sha256": "master-hash",
        "native_fixture_file_sha256": "fixture-hash",
        "special_count": 351,
        "selected_product": row,
        "selection": {
            "selected_product_number": row["product_number"],
            "candidate_count": 1,
        },
        "mapped": mapped,
        "minimal_payload": minimal,
        "native_fixture": native,
        "failed_full_payload": copy.deepcopy(minimal),
    }


class ProbeClient:
    def __init__(self, *, observable: bool):
        self.observable = observable
        self.created = False
        self.payload = None
        self.requests = []

    def _record(self, path: str, semantic: str, operation: str):
        self.requests.append(
            {
                "path": path,
                "semantic_operation": semantic,
                "operation": operation,
            }
        )

    def native_save_request_preview(self, payload):
        return ShijiuLiveClient("token", "secret").native_save_request_preview(payload)

    def categories(self):
        self._record("/shopapi/Goodtype/typeindex", "read", "category discovery")
        return {
            "code": 1,
            "data": [
                {
                    "id": 288338,
                    "type_name": "母婴用品",
                    "pid": 0,
                    "children": [
                        {"id": 294884, "type_name": "MikiHouse", "pid": 288338}
                    ],
                }
            ],
        }

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
        page=1,
        page_size=20,
        **kwargs,
    ):
        self._record("/shopapi/Goods/index", "read", "exact MIKIHOUSE SKU search")
        visible = self.created and self.observable
        if sku_code or good_name:
            rows = [self._new_row()] if visible else []
            return {"code": 1, "count": len(rows), "data": rows}
        legacy = [
            {"id": str(800000 + index), "good_name": f"legacy-{index}"}
            for index in range(286)
        ]
        rows = legacy + ([self._new_row()] if visible else [])
        start = (page - 1) * page_size
        return {"code": 1, "count": len(rows), "data": rows[start : start + page_size]}

    def upload_image(self, source_url, *, confirmation):
        assert confirmation == PROBE_CONFIRMATION
        self._record("/v1/cos/upload", "write", "official image upload")
        return "https://cdn0.19mini.com/shop/probe.jpg", {
            "code": 200,
            "data": {"url": "https://cdn0.19mini.com/shop/probe.jpg"},
        }

    def create_product_native(self, payload, *, confirmation):
        assert confirmation == PROBE_CONFIRMATION
        assert FORBIDDEN_RECOVERY_PRODUCT not in json.dumps(payload)
        self._record(
            "/shopapi/Goods/newAddGood",
            "write",
            "native minimal MIKIHOUSE product create",
        )
        self.payload = copy.deepcopy(payload)
        self.created = True
        return {"code": 200, "msg": "success", "data": []}

    def update_product_native(self, payload, *, confirmation):
        assert confirmation == PROBE_CONFIRMATION
        assert payload["id"] == 99001
        self._record(
            "/shopapi/Goods/newAddGood",
            "write",
            "native staged MIKIHOUSE product update",
        )
        self.payload = {k: copy.deepcopy(v) for k, v in payload.items() if k != "id"}
        return {"code": 200, "msg": "success", "data": {"id": 99001}}

    def product_detail(self, product_id):
        self._record("/shopapi/goods/getFormatInfo", "read", "product readback")
        sku = self.payload["sku_info"][0]
        return {
            "code": 1,
            "data": {
                "id": str(product_id),
                "good_name": self.payload["good_name"],
                "good_type": 294884,
                "state": 1,
                "is_shelf": 0,
                "master_graph": self.payload["master_graph"],
                "broadcast": self.payload["broadcast"],
                "good_details": self.payload["good_details"],
                "sku_info": [
                    {
                        "sku_id": "88001",
                        "sku_code": sku["sku_code"],
                        "price": sku["sku_price"],
                        "stock": int(float(sku["sku_stock"])),
                        "spec_son_name": sku["spec_name"].split(","),
                        "sku_thumbnail": sku["sku_thumbnail"],
                    }
                ],
            },
        }


def make_runner(tmp_path: Path, *, observable: bool):
    row = product()
    inputs = probe_inputs(row)
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(json.dumps(mapping([row])), encoding="utf-8")
    client = ProbeClient(observable=observable)
    runner = MinimalCreateProbeRunner(
        client,
        inputs,
        tmp_path / "checkpoint.json",
        mapping_path,
        tmp_path / "report.json",
        tmp_path / "candidate.json",
        tmp_path / "difference.json",
        tmp_path / "readback.json",
        confirmation=PROBE_CONFIRMATION,
    )
    runner.record_online_source_verification(
        {
            "product_number": row["product_number"],
            "name": row["name"],
            "variant_skus": [row["variants"][0]["sku"]],
            "tax_included_prices_jpy": [1650],
            "main_image_matches": True,
            "passed": True,
        }
    )
    return runner, client, mapping_path


def test_candidate_selection_is_deterministic_and_excludes_previous_product() -> None:
    first = product("17-1366-244", image_count=1)
    second = product("16-0000-001", image_count=2)
    forbidden = product(FORBIDDEN_RECOVERY_PRODUCT, image_count=1)
    state = mapping([first, second, forbidden])
    selected, evidence = select_minimal_probe_candidate(
        {"products": [forbidden, second, first]}, set(), state
    )
    assert selected["product_number"] == "17-1366-244"
    assert evidence["selected_score"] == [1, 1, "17-1366-244"]


def test_minimal_payload_uses_native_shape_one_real_sku_and_65_percent_jpy() -> None:
    inputs = probe_inputs(product())
    payload = inputs["minimal_payload"]
    native = inputs["native_fixture"]
    assert list(payload) == list(native)
    assert payload["good_type"] == 294884
    assert payload["state"] == "1" and payload["is_shelf"] == 0
    assert len(payload["sku_info"]) == 1
    assert payload["sku_info"][0]["sku_price"] == "1073.00"
    assert payload["sku_info"][0]["sku_code"] == "MIKI-17-1366-24400899999"
    assert "WAWU" not in json.dumps(payload).upper()
    assert "瓦屋" not in json.dumps(payload, ensure_ascii=False)


def test_native_request_preview_matches_audited_transport() -> None:
    payload = probe_inputs(product())["minimal_payload"]
    preview = ShijiuLiveClient("token", "secret").native_save_request_preview(payload)
    assert preview["content_type"] == "application/json;charset=UTF-8"
    assert preview["serialization"]["ensure_ascii"] is False
    assert preview["serialization"]["separators"] == [",", ":"]
    assert preview["serialization"]["body_auth_key_order"] == ["secret", "token"]
    assert "origin" not in preview["headers"]
    assert "sec-ch-ua" in preview["headers"]


def test_unobservable_minimal_create_stops_after_one_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, client, mapping_path = make_runner(tmp_path, observable=False)
    monkeypatch.setattr("mikihouse_luyao.shijiu_recovery.time.sleep", lambda _: None)
    with pytest.raises(Exception, match=r"exact verified IDs=\[\]"):
        runner.run()
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["state"] == "STOPPED_ON_PROBE_ERROR"
    assert report["requests"]["image_upload"] == 1
    assert report["requests"]["minimal_create"] == 1
    assert report["requests"]["staged_update"] == 0
    assert report["forbidden_00_1000_028_create_requests"] == 0
    assert report["legacy_products_modified"] == 0
    difference = json.loads((tmp_path / "difference.json").read_text())
    assert difference["probe_result"]["create_attempts"] == 1
    assert difference["probe_result"]["staged_update_attempts"] == 0
    assert difference["diagnostic_conclusion"]["no_guessing_or_retry"] is True
    state = json.loads(mapping_path.read_text())
    assert state["products"]["17-1366-244"]["shijiu_product_id"] is None
    before = len(client.requests)
    with pytest.raises(Exception, match="terminal"):
        runner.run()
    assert len(client.requests) == before


def test_successful_minimal_create_is_verified_before_staged_updates(
    tmp_path: Path,
) -> None:
    runner, client, mapping_path = make_runner(tmp_path, observable=True)
    report = runner.run()
    assert report["state"] == "FULL_PAYLOAD_VERIFIED"
    assert report["requests"]["minimal_create"] == 1
    assert report["requests"]["image_upload"] == 1
    assert report["requests"]["staged_update"] == 2
    assert report["shijiu_product_id"] == "99001"
    assert report["shijiu_sku_id"] == "88001"
    assert report["mapping_persisted"] is True
    assert [row["state"] for row in report["staged_updates"]] == [
        "VERIFIED",
        "SKIPPED_NO_PAYLOAD_CHANGE",
        "VERIFIED",
    ]
    state = json.loads(mapping_path.read_text())
    assert state["products"]["17-1366-244"]["shijiu_product_id"] == "99001"


def test_checked_in_live_probe_evidence_preserves_write_boundary() -> None:
    report = json.loads(
        (ROOT / "deliverables/shijiu_import/minimal_create_probe_report.json").read_text()
    )
    checkpoint = json.loads(
        (ROOT / "state/shijiu_minimal_create_probe_checkpoint.json").read_text()
    )
    mappings = json.loads((ROOT / "state/shijiu_mappings.json").read_text())
    difference = json.loads(
        (ROOT / "deliverables/shijiu_import/minimal_create_payload_diff.json").read_text()
    )
    number = report["candidate_product_number"]
    assert number == "17-1366-244"
    assert report["state"] == "STOPPED_ON_PROBE_ERROR"
    assert report["requests"]["image_upload"] == 1
    assert report["requests"]["minimal_create"] == 1
    assert report["requests"]["staged_update"] == 0
    assert report["forbidden_00_1000_028_create_requests"] == 0
    assert report["legacy_products_modified"] == 0
    assert report["batch_products_processed"] == 0
    assert report["mapping_persisted"] is False
    assert report["shijiu_product_id"] is None
    assert report["shijiu_sku_id"] is None
    assert report["post_failure_forensics"]["passed"] is True
    assert report["post_failure_forensics"]["category_scans"][0]["row_count"] == 286
    assert checkpoint["create_attempts"] == 1
    assert checkpoint["state"] == "STOPPED_ON_PROBE_ERROR"
    assert mappings["products"][number]["shijiu_product_id"] is None
    assert difference["probe_result"]["create_attempts"] == 1
    assert difference["probe_result"]["staged_update_attempts"] == 0
    assert difference["probe_result"]["api_response"]["data_shape"]["length"] == 0
    assert difference["diagnostic_conclusion"]["no_guessing_or_retry"] is True
