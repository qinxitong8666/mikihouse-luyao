from __future__ import annotations

import json

from mikihouse_luyao.shijiu_capacity_audit import (
    build_capacity_audit_report,
    target_product_capacity_metrics,
)
from mikihouse_luyao.shijiu_complex_import import UiContextReadClient


def detail(product_id: str, *, skus: int, broadcast: int, detail_pics: int) -> dict:
    carousel = [f"https://cos.example/{product_id}/c-{index}.jpg" for index in range(broadcast)]
    pictures = [f"https://cos.example/{product_id}/d-{index}.jpg" for index in range(detail_pics)]
    return {
        "code": 200,
        "msg": "success",
        "data": {
            "id": product_id,
            "broadcast": ",".join(carousel),
            "good_detail_pics": ",".join(pictures),
            "good_details": "<section>" + "".join(
                f'<img src="{url}">' for url in pictures
            ) + "</section>",
            "sku_info": [
                {"sku_code": f"SKU-{product_id}-{index}", "stock": 1}
                for index in range(skus)
            ],
        },
    }


class FakeUi:
    def __init__(self) -> None:
        self.requests = []
        self.details = {
            "1": detail("1", skus=1, broadcast=1, detail_pics=0),
            "2": detail("2", skus=14, broadcast=6, detail_pics=4),
            "3": detail("3", skus=24, broadcast=42, detail_pics=38),
        }

    def sample_context_rows(self, *, good_type=""):
        self.requests.append({
            "path": "/shopapi/Goods/index", "semantic_operation": "read"
        })
        return [
            {"id": "1", "good_name": "secret name 1"},
            {"id": "2", "good_name": "secret name 2"},
        ], {
            "declared_count": 2,
            "unique_product_count": 2,
            "all_declared_rows_enumerated": True,
            "sampling_is_deterministic": True,
        }

    def product_detail(self, product_id: str):
        self.requests.append({
            "path": "/shopapi/goods/getFormatInfo", "semantic_operation": "read"
        })
        return self.details[product_id]


def test_capacity_metrics_measure_chars_bytes_urls_images_and_skus() -> None:
    metrics = target_product_capacity_metrics(detail("1", skus=14, broadcast=6, detail_pics=4))
    assert metrics["sku_count"] == 14
    assert metrics["broadcast"]["url_count"] == 6
    assert metrics["broadcast"]["image_count"] == 6
    assert metrics["good_detail_pics"]["url_count"] == 4
    assert metrics["good_details"]["image_count"] == 4
    assert metrics["good_details"]["utf8_byte_count"] >= metrics["good_details"]["character_count"]


def test_capacity_audit_scans_visible_and_explicit_products_without_values() -> None:
    ui = FakeUi()
    report = build_capacity_audit_report(
        ui,
        legacy_product_ids={"3"},
        non_miki_test_product_ids={"1"},
        mapped_mikihouse_product_ids={"2"},
        historical_payload_rows=[{"label": "history", "metrics": {"sku_count": 1}}],
    )
    assert report["status"] == "COMPLETED_READ_ONLY"
    assert report["scope"]["unique_detail_product_count"] == 3
    assert report["target_empirical_observed_maxima"]["sku_count"]["value"] == 24
    assert report["target_sku_count_distribution"]["maximum"] == 24
    assert report["request_counts"] == {
        "read": 4,
        "goods_index": 1,
        "get_format_info": 3,
        "write": 0,
        "image_upload": 0,
        "product_create": 0,
        "update": 0,
    }
    serialized = json.dumps(report)
    assert "secret name" not in serialized
    assert "https://cos.example" not in serialized
    assert report["interpretation"]["server_hard_limit"] == (
        "NOT_PROVEN_AND_NOT_INFERRED_FROM_OBSERVED_MAXIMA"
    )


def test_ui_context_full_list_requires_declared_unique_coverage(monkeypatch) -> None:
    client = object.__new__(UiContextReadClient)
    client.base_pairs = [
        ("secret", "private"), ("page", "1"), ("page_size", "2"),
        ("good_type", ""), ("recommend", "2"), ("good_name", ""), ("push", "2"),
    ]
    client.base_form = dict(client.base_pairs)
    client.url = "https://example.test/shopapi/Goods/index&token=private"
    pages = {
        1: {"code": 200, "data": {"count": 3, "list": [{"id": 1}, {"id": 2}]}},
        2: {"code": 200, "data": {"count": 3, "list": [{"id": 3}]}},
    }
    monkeypatch.setattr(
        client,
        "_post",
        lambda url, pairs, **kwargs: pages[int(dict(pairs)["page"])],
    )
    rows, summary = client.list_context_rows()
    assert [str(row["id"]) for row in rows] == ["1", "2", "3"]
    assert summary["all_declared_rows_enumerated"] is True
    assert summary["pages_read"] == 2
