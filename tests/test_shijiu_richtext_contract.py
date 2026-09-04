from __future__ import annotations

import json
from pathlib import Path

from mikihouse_luyao.shijiu_import import _details_template
from mikihouse_luyao.shijiu_richtext_contract import (
    build_read_only_richtext_sample_audit,
    detail_field_shapes,
    rich_text_shape,
)


ROOT = Path(__file__).resolve().parents[1]


def _detail(product_id: str, details: str, pics: str = "") -> dict:
    return {
        "code": 200,
        "data": {
            "id": product_id,
            "good_details": details,
            "good_detail_pics": pics,
            "description": "internal provenance",
            "good_describe": "short summary",
        },
    }


class FakeUi:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.details = {
            "1": _detail("1", ""),
            "2": _detail("2", "plain text"),
            "3": _detail("3", "<p>light text</p>"),
            "4": _detail(
                "4",
                '<section><p>text</p><img src="https://cos.invalid/private.jpg"></section>',
                "https://cos.invalid/private.jpg",
            ),
        }

    def sample_context_rows(self, *, good_type="", sample_page_count=32):
        self.requests.append({"semantic_operation": "read", "path": "/shopapi/Goods/index"})
        return [{"id": value} for value in ("1", "2", "3", "4")], {
            "sampling_is_deterministic": True,
            "sampled_page_count": sample_page_count,
        }

    def product_detail(self, product_id: str):
        self.requests.append({
            "semantic_operation": "read",
            "path": "/shopapi/goods/getFormatInfo",
        })
        return self.details[product_id]


def test_rich_text_shape_records_structure_without_content_or_urls() -> None:
    raw = '<section><p>文字</p><img src="https://cos.example/private.jpg"></section>'
    shape = rich_text_shape(raw)
    assert shape["format"] == "HTML_OR_LIGHT_MARKUP"
    assert shape["tag_counts"] == {"img": 1, "p": 1, "section": 1}
    assert shape["image_tag_count"] == 1
    assert shape["url_count"] == 1
    assert raw not in json.dumps(shape)
    assert "cos.example" not in json.dumps(shape)


def test_detail_fields_have_distinct_target_semantics() -> None:
    shapes = detail_field_shapes(_detail("2", "<p>text</p>", "https://cos.invalid/a.jpg"))
    assert shapes["good_details"]["format"] == "HTML_OR_LIGHT_MARKUP"
    assert shapes["good_detail_pics"]["url_count"] == 1
    assert shapes["description"]["format"] == "PLAIN_TEXT"
    assert shapes["good_describe"]["format"] == "PLAIN_TEXT"


def test_read_only_audit_collects_three_nonempty_samples_and_no_values() -> None:
    ui = FakeUi()
    report = build_read_only_richtext_sample_audit(
        ui,
        legacy_product_ids=set(),
        minimum_nonempty_samples=3,
        sampled_page_count=4,
    )
    assert report["status"] == "COMPLETED_READ_ONLY"
    assert report["scope"]["nonempty_good_details_samples_collected"] == 3
    assert report["request_counts"] == {
        "read": 5,
        "goods_index": 1,
        "get_format_info": 4,
        "write": 0,
        "image_upload": 0,
        "create": 0,
        "update": 0,
    }
    assert report["safety"]["target_mutation_requests_sent"] == 0
    serialized = json.dumps(report)
    assert "plain text" not in serialized
    assert "cos.invalid" not in serialized


def test_mikihouse_details_template_is_bounded_light_html_without_images_or_urls() -> None:
    value = _details_template({
        "product_number": "10-0000-000",
        "name": "test & product",
        "brand": "MIKI HOUSE",
        "description": '<p>long description</p><img src="https://source.invalid/image.jpg">' * 200,
        "variants": [{"color": "red", "size": "100"}],
    }, ["{{SHIJIU_COS_URL:PRIVATE}}"])
    assert 0 < len(value) <= 1024
    assert "<img" not in value.lower()
    assert "http://" not in value.lower()
    assert "https://" not in value.lower()
    assert "SHIJIU_COS_URL" not in value


def test_checked_in_native_richtext_evidence_is_verified_and_sanitized() -> None:
    comparison = json.loads(
        (ROOT / "deliverables/shijiu_import/richtext_contract_comparison.json").read_text(
            encoding="utf-8"
        )
    )
    readiness = json.loads(
        (ROOT / "deliverables/shijiu_import/richtext_contract_readiness.json").read_text(
            encoding="utf-8"
        )
    )
    assert comparison["status"] == "SHIJIU_RICHTEXT_CONTRACT_VERIFIED"
    assert comparison["conclusion"]["good_details_contract"] == (
        "TEXT_OR_LIGHT_HTML_NO_IMAGE_OR_URL_MAX_1024"
    )
    target = comparison["exact_failed_mikihouse_comparison"]["target_outcome"]
    assert target["native_short_text_exactly_persisted"] is True
    assert target["native_one_image_good_details_unchanged"] is True
    assert target["native_one_detail_pic_exactly_persisted"] is True
    assert comparison["safety"]["mikihouse_write_requests"] == 0
    assert comparison["safety"]["sensitive_values_included"] is False
    assert readiness["status"] == "READY_FOR_LAST_ONE_PRODUCT_MIKIHOUSE_E2E_VALIDATION"
    assert readiness["execution_authorized"] is False
    assert readiness["pdf_special_exclusion_count"] == 351
    assert readiness["pdf_special_selected"] is False
    assert all(
        stage["operation"] != "UPDATE_GOOD_DETAILS"
        for stage in readiness["planned_stages"]
    )
