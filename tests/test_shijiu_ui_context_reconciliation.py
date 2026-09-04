from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

import pytest

from mikihouse_luyao.shijiu_import import (
    load_mapping_state,
    map_product_to_shijiu,
    reconcile_mapping_state,
    write_json_atomic,
)
from mikihouse_luyao.shijiu_live_import import LiveImportError, _resolve_payload
from mikihouse_luyao.shijiu_minimal_probe import TARGET_CATEGORY
from mikihouse_luyao.shijiu_ui_context_reconciliation import (
    EXPECTED_NAME,
    finalize_ui_context_reconciliation,
)


ROOT = Path(__file__).resolve().parents[1]


def source_product() -> dict:
    image = {"url": "https://cdn.shopify.com/reconcile.jpg", "width": 700, "height": 700}
    return {
        "product_number": "36-2001-572",
        "handle": "36-2001-572",
        "name": EXPECTED_NAME,
        "brand": "ミキハウス フォーマル",
        "product_type": "雑貨",
        "category": {"name": "Goods"},
        "tags": [],
        "description": "公式説明",
        "main_image": image,
        "ordered_images": [{"order": 1, "role": "main", "image": image}],
        "product_url": "https://www.mikihouse.co.jp/products/36-2001-572",
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


def setup(tmp_path: Path, *, with_candidate: bool = False, blocked_mutations: int = 0):
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
        "status": "RECONCILIATION_NO_UNIQUE_STRONG_EVIDENCE",
        "scope": {"product_numbers": ["36-2001-572"]},
        "create_attempts": 1,
        "create_response": {"code": 200, "data": [], "msg": "success"},
        "image_uploads": uploads,
        "request_ledger": [],
        "error": {"type": "prior", "message": "prior"},
        "shijiu_product_id": None,
        "mapping_persisted": False,
    }
    checkpoint_path = tmp_path / "checkpoint.json"
    mapping_path = tmp_path / "mapping.json"
    validation_path = tmp_path / "validation.json"
    candidate_path = tmp_path / "candidate.json"
    report_path = tmp_path / "ui-report.json"
    write_json_atomic(checkpoint_path, checkpoint)
    mapping = reconcile_mapping_state(
        load_mapping_state(tmp_path / "missing.json"), [product], TARGET_CATEGORY
    )
    write_json_atomic(mapping_path, mapping)
    write_json_atomic(validation_path, {"status": "old"})
    write_json_atomic(candidate_path, {"result": "old"})

    base_form = {
        "secret": "private-secret",
        "page": "1",
        "page_size": "20",
        "good_type": "",
        "father_type": "",
        "recommend": "",
        "good_name": "",
        "good_code": "",
        "push": "2",
        "status": "0",
        "update_start_time": "",
        "update_end_time": "",
        "create_start_time": "",
        "create_end_time": "",
        "group_id": "",
    }
    row = {"id": "99123", "good_name": EXPECTED_NAME, "state": 1}
    queries = []
    for label, good_type in (("category_294884", "294884"), ("all_categories", "")):
        request_form = {**base_form, "good_type": good_type, "good_name": EXPECTED_NAME}
        queries.append({
            "label": label,
            "good_type": good_type,
            "changed_fields_from_ui_request": (
                ["good_type", "good_name"] if good_type else ["good_name"]
            ),
            "request_form": request_form,
            "declared_count": 1 if with_candidate else 0,
            "page_size": 20,
            "pages_read": 1,
            "exact_rows": [row] if with_candidate else [],
            "responses": [{"status": 200, "json": {"code": 1, "data": [row] if with_candidate else []}}],
        })
    details = []
    if with_candidate:
        sku = payload["sku_info"][0]
        details = [{
            "product_id": "99123",
            "list_row": row,
            "request_form": {"secret": "private-secret", "id": "99123"},
            "response": {"status": 200, "json": {"code": 200, "msg": "success", "data": {
                "id": "99123",
                "good_name": payload["good_name"],
                "good_type": 294884,
                "state": 1,
                "master_graph": payload["master_graph"],
                "broadcast": payload["broadcast"],
                "good_details": payload["good_details"],
                "sku_info": [{
                    "sku_code": sku["sku_code"],
                    "price": sku["sku_price"],
                    "stock": sku["sku_stock"],
                    "spec_son_name": ["紺", "---"],
                    "sku_thumbnail": sku["sku_thumbnail"],
                }],
            }}},
        }]
    browser_payload = {
        "secret": "private-secret",
        "token": "private-token",
        "good_type": 300895,
        "good_name": "codex测试商品100",
        "supplier": "测试供应商",
        "cargo_place": "",
        "bus_region": "",
        "good_describe": "",
        "description": "TEST-SOURCE-100",
        "good_details": "",
        "spec_name": [{"spec_name": "规格", "id": 0, "son_name": [{"spec_name": "DEFAULT", "id": 1}]}],
        "sku_info": [{
            "sku_code": "CODEX-TEST-100",
            "sku_price": "100.00",
            "sku_cost_price": "100.00",
            "sku_stock": "10.00",
            "spec_name": "DEFAULT",
            "sku_thumbnail": "",
            **{field: "100.00" for field in (
                "first_level", "second_level", "third_level", "fourth_level", "fifth_level", "sixth_level"
            )},
        }],
        "master_graph": "",
        "broadcast": "",
        "good_detail_pics": "",
    }
    raw = {
        "schema_version": 1,
        "captured_at": "2026-09-04T00:00:00+00:00",
        "mode": "MIKIHOUSE_UI_CONTEXT_STRICT_READ_ONLY_RECONCILIATION",
        "target_name": EXPECTED_NAME,
        "target_category_id": 294884,
        "expected_backend_sku_code": "MIKI-36-2001-57200039999",
        "browser_create_capture_sha256": "canonical-hash",
        "browser_create_business_payload": browser_payload,
        "ui_goods_index_request": {
            "method": "POST",
            "url": "https://shijiu.wfcorp.cn/shopapi/Goods/index&token=private-token",
            "headers": {
                "accept": "application/json",
                "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
                "cookie": "private-cookie",
            },
            "post_data": urllib.parse.urlencode(base_form),
        },
        "queries": queries,
        "details": details,
        "safety": {
            "read_only_request_count": 2 + len(details),
            "target_mutation_requests_sent": 0,
            "blocked_mutation_request_count": blocked_mutations,
            "allowed_paths": ["/shopapi/Goods/index", "/shopapi/goods/getFormatInfo"],
        },
    }
    private_path = tmp_path / "shijiu-ui-context-reconciliation-test.private.json"
    private_path.write_text(json.dumps(raw), encoding="utf-8")
    return {
        "item": item,
        "payload": payload,
        "checkpoint": checkpoint,
        "special": special,
        "contract": {"browser_exact_private_evidence_sha256": "canonical-hash"},
        "paths": (checkpoint_path, mapping_path, validation_path, candidate_path, report_path),
    }


def run_finalize(tmp_path: Path, **kwargs):
    prepared = setup(tmp_path, **kwargs)
    paths = prepared["paths"]
    report = finalize_ui_context_reconciliation(
        tmp_path,
        prepared["item"],
        prepared["payload"],
        prepared["checkpoint"],
        prepared["special"],
        prepared["contract"],
        checkpoint_path=paths[0],
        mapping_path=paths[1],
        validation_report_path=paths[2],
        candidate_report_path=paths[3],
        report_path=paths[4],
    )
    return report, prepared


def test_ui_context_absence_formally_marks_historical_create_not_persisted(tmp_path: Path) -> None:
    report, prepared = run_finalize(tmp_path)
    assert report["status"] == "HISTORICAL_CREATE_NOT_PERSISTED_CONFIRMED_BY_UI_CONTEXT"
    assert report["not_persisted_confirmed"] is True
    assert report["candidate_product_ids"] == []
    assert report["safety"]["read_only_requests"] == 2
    assert report["safety"]["create_requests"] == 0
    serialized = json.dumps(report, ensure_ascii=False)
    assert "private-secret" not in serialized
    assert "private-token" not in serialized
    assert "private-cookie" not in serialized
    mapping = json.loads(prepared["paths"][1].read_text())
    assert mapping["products"]["36-2001-572"]["shijiu_product_id"] is None
    diff = report["business_value_difference"]
    good_type = next(row for row in diff["scalar_values"] if row["field"] == "good_type")
    assert good_type["browser_success_value"] == 300895
    assert good_type["canonical_mikihouse_value"] == 294884
    assert diff["sku_values"]["browser_success"][0]["sku_price"] == "100.00"
    assert diff["sku_values"]["canonical_mikihouse"][0]["sku_price"] == "1430.00"


def test_ui_context_candidate_requires_full_detail_and_keeps_sku_id_null(tmp_path: Path) -> None:
    report, prepared = run_finalize(tmp_path, with_candidate=True)
    assert report["status"] == "RECONCILED_READBACK_VERIFIED_UI_CONTEXT"
    assert report["candidate_product_ids"] == ["99123"]
    assert report["verified_product_ids"] == ["99123"]
    assert report["shijiu_sku_id"] is None
    mapping = json.loads(prepared["paths"][1].read_text())
    row = mapping["products"]["36-2001-572"]
    assert row["shijiu_product_id"] == "99123"
    assert row["variants"]["36-2001-57200039999"]["shijiu_sku_id"] is None
    second = finalize_ui_context_reconciliation(
        tmp_path,
        prepared["item"],
        prepared["payload"],
        json.loads(prepared["paths"][0].read_text()),
        prepared["special"],
        prepared["contract"],
        checkpoint_path=prepared["paths"][0],
        mapping_path=prepared["paths"][1],
        validation_report_path=prepared["paths"][2],
        candidate_report_path=prepared["paths"][3],
        report_path=prepared["paths"][4],
    )
    assert second == report


def test_ui_context_rejects_any_mutation_evidence(tmp_path: Path) -> None:
    prepared = setup(tmp_path, blocked_mutations=1)
    paths = prepared["paths"]
    with pytest.raises(LiveImportError, match="zero-mutation"):
        finalize_ui_context_reconciliation(
            tmp_path,
            prepared["item"],
            prepared["payload"],
            prepared["checkpoint"],
            prepared["special"],
            prepared["contract"],
            checkpoint_path=paths[0],
            mapping_path=paths[1],
            validation_report_path=paths[2],
            candidate_report_path=paths[3],
            report_path=paths[4],
        )


def test_checked_in_ui_context_evidence_is_verified_and_sanitized() -> None:
    report = json.loads((
        ROOT / "deliverables/shijiu_import/canonical_create_ui_context_reconciliation_report.json"
    ).read_text(encoding="utf-8"))
    assert report["status"] == "RECONCILED_READBACK_VERIFIED_UI_CONTEXT"
    assert report["candidate_product_ids"] == report["verified_product_ids"] == ["9358233"]
    assert report["shijiu_product_id"] == "9358233"
    assert report["shijiu_sku_id"] is None
    assert report["candidate_validations"] == [{
        "product_id": "9358233",
        "passed": True,
        "ui_detail_success_contract": {"code": 200, "msg": "success"},
        "backend_sku_code": "MIKI-36-2001-57200039999",
        "price_jpy": 1430,
        "category_id": 294884,
        "specification_verified": True,
        "images_verified": True,
        "is_shelf_exposed": False,
        "is_shelf_policy": "missing accepted only for UI-context; explicit nonzero rejected",
    }]
    assert report["ui_context"]["auth_context"] == {
        "query_token_present": True,
        "body_token_present": False,
        "body_secret_present": True,
        "query_body_token_equal": None,
        "values_included": False,
    }
    assert report["ui_context"]["filter_context"]["recommend"] == "2"
    assert report["ui_context"]["filter_context"]["push"] == "2"
    assert report["safety"]["read_only_requests"] == 36
    assert sum(report["safety"][key] for key in (
        "create_requests", "image_upload_requests", "update_requests", "other_target_mutations"
    )) == 0
    diff = report["business_value_difference"]
    scalars = {row["field"]: row for row in diff["scalar_values"]}
    assert scalars["good_type"]["browser_success_value"] == 294880
    assert scalars["good_type"]["canonical_mikihouse_value"] == 294884
    assert diff["sku_values"]["browser_success"][0]["sku_stock"] == "140.00"
    assert diff["sku_values"]["canonical_mikihouse"][0]["sku_stock"] == "1.00"
    assert report["diagnostic_conclusion"]["historical_create_persisted"] is True
    assert report["diagnostic_conclusion"]["business_value_differences_are_causal"] == "NOT_PROVEN"
    assert report["sensitive_values_included"] is False
