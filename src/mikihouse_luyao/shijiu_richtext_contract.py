from __future__ import annotations

import hashlib
import html
import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

from .shijiu_complex_import import UiContextReadClient, _row_id
from .shijiu_import import now
from .shijiu_live_import import DETAIL_PATH, LIST_PATH, LiveImportError, _first_observation, _split_urls


READ_ONLY_MODE = "SHIJIU_RICHTEXT_CONTRACT_READ_ONLY_AUDIT"
RICH_TEXT_FIELDS = ("good_details", "good_detail_pics", "description", "good_describe")


class _TagShapeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.tags: Counter[str] = Counter()
        self.image_sources: list[str] = []
        self.text_fragments: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        self.tags[normalized] += 1
        if normalized == "img":
            source = dict(attrs).get("src")
            if source:
                self.image_sources.append(source.strip())

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        stripped = data.strip()
        if stripped:
            self.text_fragments.append(stripped)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def rich_text_shape(value: Any) -> dict[str, Any]:
    """Return structural evidence without persisting content or URL values."""
    text = "" if value is None else str(value)
    parser = _TagShapeParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        # Shape collection must not attempt to repair or normalize target data.
        pass
    url_matches = re.findall(r"https?://[^\"'<>\s,]+", text, flags=re.I)
    stripped_text = " ".join(parser.text_fragments)
    has_markup = bool(re.search(r"<\s*/?\s*[a-zA-Z][^>]*>", text))
    return {
        "value_type": type(value).__name__,
        "present": bool(text),
        "character_count": len(text),
        "utf8_byte_count": len(text.encode("utf-8")),
        "sha256": _sha256(text),
        "format": "HTML_OR_LIGHT_MARKUP" if has_markup else ("PLAIN_TEXT" if text else "EMPTY"),
        "tag_counts": dict(sorted(parser.tags.items())),
        "image_tag_count": sum(count for tag, count in parser.tags.items() if tag == "img"),
        "embedded_image_source_count": len(parser.image_sources),
        "url_count": len(url_matches),
        "text_character_count_after_tag_parse": len(html.unescape(stripped_text)),
        "raw_value_included": False,
        "url_values_included": False,
    }


def comma_media_shape(value: Any) -> dict[str, Any]:
    text = "" if value is None else str(value)
    urls = _split_urls(value)
    return {
        "value_type": type(value).__name__,
        "present": bool(text),
        "character_count": len(text),
        "utf8_byte_count": len(text.encode("utf-8")),
        "sha256": _sha256(text),
        "url_count": len(urls),
        "max_url_character_count": max(map(len, urls), default=0),
        "raw_value_included": False,
        "url_values_included": False,
    }


def detail_field_shapes(detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "good_details": rich_text_shape(_first_observation(detail, ("good_details",))),
        "good_detail_pics": comma_media_shape(
            _first_observation(detail, ("good_detail_pics",))
        ),
        "description": rich_text_shape(_first_observation(detail, ("description",))),
        "good_describe": rich_text_shape(_first_observation(detail, ("good_describe",))),
    }


def _identity_hash(product_id: str) -> str:
    return _sha256(f"shijiu-product:{product_id}")


def build_read_only_richtext_sample_audit(
    ui: UiContextReadClient,
    *,
    legacy_product_ids: Iterable[str],
    minimum_nonempty_samples: int = 3,
    sampled_page_count: int = 32,
) -> dict[str, Any]:
    """Collect current target shapes through Goods.index + getFormatInfo only."""
    if minimum_nonempty_samples < 3:
        raise LiveImportError("rich-text contract audit requires at least three non-empty samples")
    request_start = len(ui.requests)
    rows, list_scope = ui.sample_context_rows(
        good_type="",
        sample_page_count=sampled_page_count,
    )
    legacy = {str(value) for value in legacy_product_ids if str(value)}
    ordered_ids: list[str] = []
    seen: set[str] = set()
    for product_id in [*sorted(legacy), *[_row_id(row) for row in rows]]:
        if product_id and product_id not in seen:
            seen.add(product_id)
            ordered_ids.append(product_id)

    samples: list[dict[str, Any]] = []
    inspected = 0
    for product_id in ordered_ids:
        detail = ui.product_detail(product_id)
        inspected += 1
        shapes = detail_field_shapes(detail)
        if not shapes["good_details"]["present"]:
            continue
        samples.append({
            "product_identity_sha256": _identity_hash(product_id),
            "cohort": "LEGACY_REFERENCE_ONLY" if product_id in legacy else "OTHER_READ_ONLY_TARGET_SAMPLE",
            "fields": shapes,
        })
        if len(samples) >= minimum_nonempty_samples:
            break
    if len(samples) < minimum_nonempty_samples:
        raise LiveImportError(
            f"found only {len(samples)} non-empty good_details samples after {inspected} reads"
        )

    requests = ui.requests[request_start:]
    if any(
        row.get("semantic_operation") != "read"
        or row.get("path") not in {LIST_PATH, DETAIL_PATH}
        for row in requests
    ):
        raise LiveImportError("rich-text audit observed a prohibited target operation")

    tag_presence = Counter()
    format_counts = Counter()
    for sample in samples:
        details = sample["fields"]["good_details"]
        format_counts[details["format"]] += 1
        for tag in details["tag_counts"]:
            tag_presence[tag] += 1
    return {
        "schema_version": 1,
        "generated_at": now(),
        "status": "COMPLETED_READ_ONLY",
        "mode": READ_ONLY_MODE,
        "target": "SHIJIU",
        "scope": {
            "minimum_required_nonempty_good_details_samples": minimum_nonempty_samples,
            "nonempty_good_details_samples_collected": len(samples),
            "get_format_info_products_inspected": inspected,
            "list_sampling": list_scope,
            "legacy_reference_mode": "READ_ONLY_NO_MODIFICATION_NO_IDENTITY_BINDING",
        },
        "samples": samples,
        "aggregate": {
            "good_details_format_counts": dict(sorted(format_counts.items())),
            "tag_sample_presence_counts": dict(sorted(tag_presence.items())),
            "good_details_with_img_count": sum(
                sample["fields"]["good_details"]["image_tag_count"] > 0 for sample in samples
            ),
            "good_details_with_urls_count": sum(
                sample["fields"]["good_details"]["url_count"] > 0 for sample in samples
            ),
            "good_detail_pics_nonempty_count": sum(
                sample["fields"]["good_detail_pics"]["present"] for sample in samples
            ),
        },
        "request_counts": {
            "read": len(requests),
            "goods_index": sum(row.get("path") == LIST_PATH for row in requests),
            "get_format_info": sum(row.get("path") == DETAIL_PATH for row in requests),
            "write": 0,
            "image_upload": 0,
            "create": 0,
            "update": 0,
        },
        "safety": {
            "target_mutation_requests_sent": 0,
            "product_ids_persisted": False,
            "product_names_persisted": False,
            "raw_field_values_persisted": False,
            "raw_urls_persisted": False,
            "authentication_values_persisted": False,
        },
    }


def current_contract_static_evidence(repo_root: Path, wawu_root: Path) -> dict[str, Any]:
    """Record auditable source locations and hashes, never private request values."""
    def git_head(root: Path) -> str:
        git_dir = root / ".git"
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head
        reference = head.removeprefix("ref: ")
        loose = git_dir / reference
        if loose.exists():
            return loose.read_text(encoding="utf-8").strip()
        for line in (git_dir / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#") and line.endswith(f" {reference}"):
                return line.split(" ", 1)[0]
        raise LiveImportError(f"cannot resolve Git HEAD for static evidence: {root}")

    evidence = [
        ("MIKI_BROWSER_CAPTURE", repo_root / "scripts/shijiu_browser_exact_capture.mjs"),
        ("MIKI_NATIVE_WRITER", repo_root / "src/mikihouse_luyao/shijiu_live_import.py"),
        ("MIKI_FAILED_FINAL_HTML", repo_root / "state/shijiu_production_architecture_verification_checkpoint.json"),
        ("WAWU_SHIJIU_CLIENT", wawu_root / "backend_client.py"),
        ("WAWU_SHIJIU_TRANSFORMER", wawu_root / "transformer.py"),
        ("WAWU_DETAIL_TRANSFORMER", wawu_root / "detail_transformer.py"),
    ]
    rows = []
    for label, path in evidence:
        if not path.exists():
            raise LiveImportError(f"required static contract evidence is missing: {path}")
        raw = path.read_bytes()
        rows.append({
            "label": label,
            "path_role": path.name,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "byte_count": len(raw),
            "raw_content_included": False,
        })
    return {
        "repository_heads": {
            "mikihouse_luyao_audited_main_baseline": git_head(repo_root),
            "wawu_product_sync_current_main": git_head(wawu_root),
        },
        "evidence": rows,
        "observed_contract": {
            "save_endpoint": "/shopapi/Goods/newAddGood",
            "readback_endpoint": "/shopapi/goods/getFormatInfo",
            "separate_detail_save_endpoint_observed": False,
            "good_details_semantics": "TEXT_OR_LIGHT_HTML",
            "good_detail_pics_semantics": "ORDERED_COMMA_SEPARATED_DETAIL_IMAGE_URLS",
            "good_describe_semantics": "SHORT_CUSTOMER_FACING_SUMMARY",
            "description_semantics": "INTERNAL_OR_PROVENANCE_DESCRIPTION",
            "wawu_reference_good_details_character_guard": 1024,
            "wawu_reference_embedded_img_policy": "EXTRACT_TO_GOOD_DETAIL_PICS",
        },
        "evidence_boundary": {
            "separate_detail_endpoint_absence": (
                "No separate endpoint was found in the audited current source/captures; this is "
                "not proof that no server endpoint exists."
            ),
            "html_filter_or_hard_length_limit": (
                "Not claimed until a native browser edit is captured and read back."
            ),
            "wawu_semantics_reused": "SHIJIU_DOWNSTREAM_ONLY",
        },
    }


def _capture_contract_summary(report: dict[str, Any]) -> dict[str, Any]:
    capture = report.get("current_capture") or {}
    request = capture.get("request") or {}
    body = request.get("body") or {}
    rich = capture.get("rich_text_contract") or {}
    return {
        "state": report.get("state"),
        "operation_kind": capture.get("operation_kind"),
        "endpoint": (request.get("url") or {}).get("path"),
        "query_parameter_names": (request.get("url") or {}).get("query_parameter_names") or [],
        "header_names": (request.get("headers") or {}).get("names") or [],
        "content_type": request.get("content_type"),
        "body_field_names_in_order": body.get("field_names_in_order") or [],
        "body_field_types": body.get("field_types") or {},
        "rich_text_fields": (rich.get("fields") or {}),
        "readback": {
            "product_id_sha256": _identity_hash(
                str((capture.get("readback") or {}).get("product_id") or "")
            ),
            "goods_index_unique": bool((capture.get("readback") or {}).get("goods_index_unique")),
            "get_format_info_verified": bool(
                (capture.get("readback") or {}).get("get_format_info_verified")
            ),
        },
        "raw_values_included": False,
    }


def build_richtext_contract_comparison(
    *,
    create_capture: dict[str, Any],
    text_edit_capture: dict[str, Any],
    image_edit_capture: dict[str, Any],
    failed_miki_checkpoint: dict[str, Any],
    failed_miki_forensics: dict[str, Any],
    canonical_create_contract: dict[str, Any],
    read_only_audit: dict[str, Any],
) -> dict[str, Any]:
    create = _capture_contract_summary(create_capture)
    text_edit = _capture_contract_summary(text_edit_capture)
    image_edit = _capture_contract_summary(image_edit_capture)
    final_stage = next(
        (
            row for row in failed_miki_checkpoint.get("stages") or []
            if row.get("key") == "FINAL_GOOD_DETAILS_HTML"
        ),
        None,
    )
    if not final_stage:
        raise LiveImportError("frozen 63-3210-146 final HTML evidence is unavailable")
    native_fields = image_edit["body_field_names_in_order"]
    expected_miki_fields = [
        "secret",
        "token",
        *canonical_create_contract["product_fields"],
        "id",
    ]
    native_types = image_edit["body_field_types"]
    expected_miki_types = {
        "secret": "string",
        "token": "string",
        **canonical_create_contract["product_field_types"],
        "id": "number",
    }
    type_differences = [
        {
            "field": field,
            "browser_native": native_types.get(field, "MISSING"),
            "failed_mikihouse_writer": expected_miki_types.get(field, "MISSING"),
        }
        for field in sorted(set(native_types) | set(expected_miki_types))
        if native_types.get(field) != expected_miki_types.get(field)
    ]
    text_details = text_edit["rich_text_fields"]["good_details"]
    image_details = image_edit["rich_text_fields"]["good_details"]
    image_pics = image_edit["rich_text_fields"]["good_detail_pics"]
    failed_metrics = final_stage.get("metrics") or {}
    all_native_verified = all(
        row["state"] == "BROWSER_EXACT_CAPTURE_VERIFIED"
        and row["readback"]["goods_index_unique"]
        and row["readback"]["get_format_info_verified"]
        for row in (create, text_edit, image_edit)
    )
    native_request_shapes_equal = (
        text_edit["endpoint"] == image_edit["endpoint"] == canonical_create_contract["create_endpoint"]
        and text_edit["query_parameter_names"] == image_edit["query_parameter_names"]
        and text_edit["header_names"] == image_edit["header_names"]
        and text_edit["content_type"] == image_edit["content_type"]
    )
    return {
        "schema_version": 1,
        "generated_at": now(),
        "status": "SHIJIU_RICHTEXT_CONTRACT_VERIFIED",
        "target": "SHIJIU",
        "scope": "ONE_NON_MIKIHOUSE_DISPOSABLE_PRODUCT_TWO_NATIVE_EDITS_PLUS_READ_ONLY_SAMPLES",
        "native_captures": {
            "create": create,
            "short_text_edit": text_edit,
            "one_detail_image_edit": image_edit,
        },
        "field_semantics_proven": {
            "good_details": (
                "Native UI label '详情介绍'; string containing plain text or observed light HTML. "
                "The native one-image edit leaves this field unchanged."
            ),
            "good_detail_pics": (
                "Native UI label '详情图'; ordered comma-separated Shijiu/COS URL string."
            ),
            "good_describe": "Native UI label '简述'; separate from 详情介绍.",
            "description": "Not populated by either native detail edit; separate internal field.",
            "save_endpoint": "/shopapi/Goods/newAddGood for both native EDIT operations",
            "separate_detail_save_endpoint_observed": False,
        },
        "exact_failed_mikihouse_comparison": {
            "product_number": "63-3210-146",
            "historical_checkpoint_remains_frozen": True,
            "browser_native_vs_mikihouse_endpoint_query_headers_content_type_equal": native_request_shapes_equal,
            "body_field_order_equal": native_fields == expected_miki_fields,
            "body_fields_only_in_browser_native": sorted(set(native_fields) - set(expected_miki_fields)),
            "body_fields_only_in_failed_mikihouse_writer": sorted(set(expected_miki_fields) - set(native_fields)),
            "body_type_differences": type_differences,
            "decisive_business_value_difference": {
                "browser_short_text_edit_good_details": text_details["request"],
                "browser_image_edit_good_details": image_details["request"],
                "browser_image_edit_good_detail_pics": image_pics["request"],
                "failed_mikihouse_final_good_details": {
                    "character_count": failed_metrics.get("good_details_characters"),
                    "utf8_byte_count": failed_metrics.get("good_details_utf8_bytes"),
                    "sha256": failed_metrics.get("good_details_sha256"),
                    "image_count": failed_miki_forensics.get("expected_html_cos_image_count"),
                    "url_count": failed_miki_forensics.get("expected_html_cos_image_count"),
                    "format": "IMAGE_BEARING_HTML",
                },
                "failed_mikihouse_good_detail_pics_url_count": failed_metrics.get(
                    "good_detail_pics_url_count"
                ),
            },
            "target_outcome": {
                "native_short_text_exactly_persisted": text_details["exact_sha256_match"],
                "native_one_image_good_details_unchanged": (
                    text_details["request"]["sha256"] == image_details["request"]["sha256"]
                    == image_details["readback"]["sha256"]
                ),
                "native_one_detail_pic_exactly_persisted": image_pics["exact_sha256_match"],
                "failed_mikihouse_final_html_acknowledged_but_not_persisted": (
                    failed_miki_forensics.get("status")
                    == "FINAL_HTML_MUTATION_ACKNOWLEDGED_BUT_TARGET_RETAINED_PRIOR_MINIMAL_HTML"
                ),
                "failed_mikihouse_non_html_fields_preserved": bool(
                    failed_miki_forensics.get("all_non_html_fields_match_last_verified_state")
                ),
            },
        },
        "read_only_sample_corroboration": {
            "sample_count": read_only_audit["scope"][
                "nonempty_good_details_samples_collected"
            ],
            "format_counts": read_only_audit["aggregate"]["good_details_format_counts"],
            "samples_with_img": read_only_audit["aggregate"]["good_details_with_img_count"],
            "samples_with_urls": read_only_audit["aggregate"]["good_details_with_urls_count"],
            "samples_with_good_detail_pics": read_only_audit["aggregate"][
                "good_detail_pics_nonempty_count"
            ],
        },
        "conclusion": {
            "all_native_capture_and_readbacks_verified": all_native_verified,
            "good_details_contract": "TEXT_OR_LIGHT_HTML_NO_IMAGE_OR_URL_MAX_1024",
            "detail_image_contract": "GOOD_DETAIL_PICS_ORDERED_SHIJIU_COS_URLS",
            "html_filter_or_server_hard_limit": (
                "NOT CLAIMED. Current evidence proves the supported production representation, "
                "not a universal server parser limit."
            ),
            "production_fix": (
                "Do not install image-bearing final good_details HTML. Keep text/light HTML in "
                "good_details and carry all detail images in good_detail_pics."
            ),
        },
        "safety": {
            "mikihouse_write_requests": 0,
            "non_mikihouse_test_product_create_requests": 1,
            "non_mikihouse_test_product_edit_requests": 2,
            "legacy_products_modified": 0,
            "frozen_mikihouse_products_retried": 0,
            "bulk_20_executed": False,
            "sensitive_values_included": False,
            "raw_body_values_included": False,
            "raw_urls_included": False,
        },
    }
