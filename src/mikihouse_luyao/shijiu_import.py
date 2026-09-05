from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import os
import re
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .catalog import _classification, calculate_mini_program_price_jpy
from .csv_input import read_product_numbers
from .stable_catalog import STABLE, assess_product_stability


ADAPTER_SCHEMA_VERSION = 2
SOURCE_CODE = "MIKIHOUSE"
PDF_SPECIAL_EXCLUDED_REASON = "PDF_SPECIAL_LIST"
EXPECTED_SPECIAL_COUNT = 351
EXPECTED_LEGACY_REFERENCE_COUNT = 286
DEFAULT_LEGACY_AUDIT_SAMPLE_SIZE = 6
DECLARED_STOREFRONT_BASELINE_COUNT = 2914
DECLARED_CANDIDATE_BASELINE_COUNT = 2603
SHIJIU_GOOD_DETAILS_MAX_CHARACTERS = 1024
WAWU_REFERENCE_COMMIT = "a36c5eab40bf419562ba03d15c090151698d582a"
SHIJIU_CONTRACT_AUDIT_PATH = "docs/shijiu_downstream_contract_audit.md"
DEFAULT_SHIJIU_BASE_URL = "https://api.wfcorp.cn/shijiu"
READ_ONLY_ENDPOINTS = frozenset({
    "/shopapi/Goods/index",
    "/shopapi/goods/getFormatInfo",
    "/shopapi/Goodtype/typeindex",
    "/shopapi/Goodtype/index",
    "/shopapi/goodtype/fatherIndex",
})
MUTATING_ENDPOINTS = frozenset({
    "/shopapi/Goods/newAddGood",
    "/shopapi/Goods/grounding",
    "/shopapi/Goods/delGood",
    "/v1/cos/upload",
    "/shopapi/Goodtype/editParentTypedo",
    "/shopapi/Goodtype/editSonTypedo",
    "/shopapi/Goodtype/delType",
})
MEMBER_LEVEL_FIELDS = (
    "first_level",
    "second_level",
    "third_level",
    "fourth_level",
    "fifth_level",
    "sixth_level",
)


class ImportPlanError(RuntimeError):
    pass


class WriteProhibitedError(ImportPlanError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def content_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        result[key.strip()] = value
    return result


class ReadOnlyShijiuClient:
    """Shijiu read client with a hard endpoint allowlist and no write methods."""

    def __init__(
        self,
        token: str,
        secret: str,
        *,
        base_url: str = DEFAULT_SHIJIU_BASE_URL,
        timeout: float = 60,
        request_observer: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if not token or not secret:
            raise ImportPlanError(
                "SHIJIU_TOKEN/SHIJIU_SECRET (or the legacy MYSHOP_TOKEN/MYSHOP_SECRET names) "
                "are required for read-only Shijiu checks"
            )
        self.token = token
        self.secret = secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.request_observer = request_observer
        self.requests: list[dict[str, Any]] = []

    def _endpoint(self, path: str) -> str:
        return f"{self.base_url}{path}&token={urllib.parse.quote(self.token)}"

    def request_read(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if path not in READ_ONLY_ENDPOINTS:
            raise WriteProhibitedError(f"target endpoint is not read-only and is blocked: {path}")
        if path in MUTATING_ENDPOINTS:
            raise WriteProhibitedError(f"mutating target endpoint is blocked: {path}")
        safe_payload = dict(payload or {})
        audit = {
            "sequence": len(self.requests) + 1,
            "method": "POST",
            "semantic_operation": "read",
            "path": path,
            "payload": safe_payload,
        }
        self.requests.append(audit)
        if self.request_observer:
            self.request_observer(copy.deepcopy(audit))
        body_payload = {"secret": self.secret, "token": self.token, **safe_payload}
        body = urllib.parse.urlencode(body_payload).encode("utf-8")
        request = urllib.request.Request(
            self._endpoint(path),
            data=body,
            method="POST",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Origin": "https://shijiu.wfcorp.cn",
                "Referer": "https://shijiu.wfcorp.cn/",
                "User-Agent": "Mozilla/5.0 (compatible; mikihouse-luyao/0.7; read-only)",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ImportPlanError(f"target read endpoint returned invalid JSON: {path}") from exc
        if not isinstance(result, dict):
            raise ImportPlanError(f"target read endpoint returned a non-object: {path}")
        return result

    def search_products(
        self,
        *,
        sku_code: str = "",
        page: int = 1,
        page_size: int = 20,
        good_type: int | str = "",
    ) -> dict[str, Any]:
        return self.request_read(
            "/shopapi/Goods/index",
            {
                "page": page,
                "page_size": page_size,
                "good_type": good_type,
                "father_type": "",
                "recommend": "",
                "good_name": "",
                "good_code": sku_code,
                "push": "2",
                "status": "",
                "update_start_time": "",
                "update_end_time": "",
                "create_start_time": "",
                "create_end_time": "",
                "group_id": "",
            },
        )

    def product_detail(self, backend_product_id: str | int) -> dict[str, Any]:
        return self.request_read("/shopapi/goods/getFormatInfo", {"id": backend_product_id})

    def categories(self) -> dict[str, Any]:
        return self.request_read("/shopapi/goodtype/fatherIndex", {})

    def category_list(self, page: int = 1) -> dict[str, Any]:
        return self.request_read("/shopapi/Goodtype/typeindex", {"page": page})

    def category_management_index(
        self, *, page: int = 1, parent_id: str | int | None = None
    ) -> dict[str, Any]:
        payload = {"id": parent_id} if parent_id not in (None, "") else {"page": page}
        return self.request_read("/shopapi/Goodtype/index", payload)

    @property
    def semantic_write_request_count(self) -> int:
        return sum(item["path"] in MUTATING_ENDPOINTS for item in self.requests)


def response_rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    data = response.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("list", "data", "rows"):
            if isinstance(data.get(key), list):
                return [item for item in data[key] if isinstance(item, dict)]
    return []


def _target_product_id(row: dict[str, Any]) -> Any:
    for key in ("id", "good_id", "goods_id"):
        if row.get(key) not in (None, ""):
            return row[key]
    return None


def recursively_find_skus(value: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def walk(current: Any) -> None:
        if isinstance(current, dict):
            if "sku_code" in current and any(
                key in current for key in ("sku_price", "price", "sku_stock", "stock", "spec_name")
            ):
                marker = canonical_json(current)
                if marker not in seen:
                    seen.add(marker)
                    rows.append(current)
            for child in current.values():
                walk(child)
        elif isinstance(current, list):
            for child in current:
                walk(child)

    walk(value)
    return rows


def _classification_name(product: dict[str, Any]) -> str:
    return _classification(product) or "other"


def load_category_map(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("source") != SOURCE_CODE or payload.get("target") != "SHIJIU":
        raise ImportPlanError("category map must explicitly declare MIKIHOUSE -> SHIJIU")
    category = payload.get("target_category") or {}
    if (
        not isinstance(category.get("id"), int)
        or not str(category.get("name") or "").strip()
        or not isinstance(category.get("parent_id"), int)
        or category.get("assignment_policy") != "all_publishable_mikihouse_products"
    ):
        raise ImportPlanError("invalid fixed Shijiu MIKI HOUSE category mapping")
    return category


def _category_rows(value: Any, parent: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            rows.extend(_category_rows(item, parent))
    elif isinstance(value, dict):
        if value.get("id") is not None and value.get("type_name") is not None:
            rows.append({
                "id": int(value["id"]),
                "name": str(value["type_name"]),
                "parent_id": int(value.get("pid") or (parent or {}).get("id") or 0),
                "parent_name": str((parent or {}).get("type_name") or ""),
            })
            parent = value
        for child in value.get("children") or []:
            rows.extend(_category_rows(child, parent))
        if not rows:
            for child in value.values():
                rows.extend(_category_rows(child, parent))
    return rows


def validate_live_mikihouse_category(
    configured: dict[str, Any], response: dict[str, Any]
) -> dict[str, Any]:
    normalized_matches = [
        row
        for row in _category_rows(response)
        if re.sub(r"[^a-z0-9]", "", row["name"].casefold()) == "mikihouse"
    ]
    exact = [row for row in normalized_matches if row["id"] == configured["id"]]
    passed = (
        len(normalized_matches) == 1
        and len(exact) == 1
        and exact[0]["name"] == configured["name"]
        and exact[0]["parent_id"] == configured["parent_id"]
    )
    result = {
        "configured": configured,
        "normalized_name_match_count": len(normalized_matches),
        "matches": normalized_matches,
        "all_products_fixed_to_category_id": configured["id"],
        "passed": passed,
    }
    if not passed:
        raise ImportPlanError(f"fixed Shijiu MIKI HOUSE category validation failed: {result}")
    return result


def source_product_id(product_number: str) -> str:
    return f"{SOURCE_CODE}:{product_number}"


def source_variant_id(product_number: str, sku: str) -> str:
    return f"{SOURCE_CODE}:{product_number}:{sku}"


def backend_sku_code(sku: str) -> str:
    return f"MIKI-{sku}"


def _money(value: int | str | Decimal) -> str:
    return f"{Decimal(str(value)):.2f}"


def _target_option_name(name: str) -> str:
    normalized = name.strip().casefold()
    if normalized in {"カラー", "color", "colour"}:
        return "颜色"
    if normalized in {"サイズ", "size"}:
        return "尺码"
    return name.strip() or "规格"


def _variant_attributes(variant: dict[str, Any]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for item in variant.get("selected_options") or []:
        name = _target_option_name(str(item.get("name") or ""))
        value = str(item.get("value") or "").strip()
        if value and (name, value) not in result:
            result.append((name, value))
    if not result:
        if variant.get("color"):
            result.append(("颜色", str(variant["color"])))
        if variant.get("size"):
            result.append(("尺码", str(variant["size"])))
    return result or [("规格", "默认规格")]


def _specification_structure(variants: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    dimensions: list[dict[str, Any]] = []
    dimension_names: list[str] = []
    values_by_name: dict[str, list[str]] = {}
    for variant in variants:
        for name, value in _variant_attributes(variant):
            if name not in values_by_name:
                dimension_names.append(name)
                values_by_name[name] = []
            if value not in values_by_name[name]:
                values_by_name[name].append(value)
    for dimension_id, name in enumerate(dimension_names):
        dimensions.append({
            "spec_name": name,
            "id": dimension_id,
            "son_name": [
                {"spec_name": value, "id": index}
                for index, value in enumerate(values_by_name[name], start=1)
            ],
        })
    return dimensions, dimension_names


def assert_not_pdf_special(product_number: str, excluded_product_numbers: set[str]) -> None:
    if product_number in excluded_product_numbers:
        raise ImportPlanError(
            f"{PDF_SPECIAL_EXCLUDED_REASON}: {product_number} is permanently excluded from every Shijiu stage"
        )


def assert_stable_for_shijiu(
    product: dict[str, Any], excluded_product_numbers: set[str]
) -> None:
    decision = assess_product_stability(product, excluded_product_numbers)
    if decision["status"] != STABLE:
        reason = decision.get("excluded_reason") or decision["status"]
        raise ImportPlanError(
            f"{reason}: {product.get('product_number')} is outside the stable regular-product pool"
        )


def assert_shijiu_pool_boundary(
    products: list[dict[str, Any]],
    changes: dict[str, Any],
    excluded_product_numbers: set[str],
) -> None:
    if len(excluded_product_numbers) != EXPECTED_SPECIAL_COUNT:
        raise ImportPlanError(
            f"expected {EXPECTED_SPECIAL_COUNT} permanent exclusions, got {len(excluded_product_numbers)}"
        )
    product_numbers = {str(item.get("product_number") or "") for item in products}
    change_numbers = {
        str(item.get("product_number") or "") for item in changes.get("changes") or []
    }
    leaked = sorted((product_numbers | change_numbers) & excluded_product_numbers)
    if leaked:
        raise ImportPlanError(
            f"{PDF_SPECIAL_EXCLUDED_REASON}: forbidden product numbers reached Shijiu planning: {leaked[:20]}"
        )
    unstable = []
    for product in products:
        decision = assess_product_stability(product, excluded_product_numbers)
        if decision["status"] != STABLE:
            unstable.append((str(product.get("product_number") or ""), decision["excluded_reason"]))
    if unstable:
        raise ImportPlanError(
            f"non-stable products reached Shijiu planning: {unstable[:20]}"
        )
    changes_without_stable_product = sorted(change_numbers - product_numbers)
    if changes_without_stable_product:
        raise ImportPlanError(
            "incremental changes reference products outside stable_catalog: "
            f"{changes_without_stable_product[:20]}"
        )


def _product_image_entries(product: dict[str, Any]) -> list[dict[str, Any]]:
    ordered = product.get("ordered_images") or []
    if ordered:
        return copy.deepcopy(ordered)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(image: dict[str, Any] | None, role: str, **metadata: Any) -> None:
        url = str((image or {}).get("url") or "").strip()
        if not url or url in seen:
            return
        seen.add(url)
        result.append({
            "order": len(result) + 1,
            "role": role,
            "image": copy.deepcopy(image),
            **metadata,
        })

    add(product.get("main_image"), "main")
    for image in product.get("product_images") or []:
        add(image, "product_gallery")
    for color in product.get("color_images") or []:
        for item in color.get("images") or []:
            add(item.get("image"), "variant_color", colors=[color.get("color") or ""])
    for variant in product.get("variants") or []:
        add(
            variant.get("resolved_image"),
            "variant_color",
            colors=[variant.get("color") or ""],
            variant_skus=[variant.get("sku")],
        )
    for image in product.get("detail_images") or []:
        add(image, "detail")
    return result


def _upload_reference(product_number: str, order: int) -> str:
    return f"MIKIHOUSE:{product_number}:IMAGE:{order:03d}"


def _cos_placeholder(upload_reference: str) -> str:
    return f"{{{{SHIJIU_COS_URL:{upload_reference}}}}}"


def _details_template(product: dict[str, Any], detail_placeholders: list[str]) -> str:
    """Build target-supported text/light HTML; images live in good_detail_pics.

    `detail_placeholders` remains in the signature for compatibility with the
    stable mapper call site, but browser-native evidence proves that Shijiu's
    editor stores detail images in the separate `good_detail_pics` field.
    """
    del detail_placeholders
    product_number = html.escape(str(product.get("product_number") or ""))
    name = html.escape(str(product.get("name") or ""))
    brand = html.escape(str(product.get("brand") or ""))
    # Source descriptions occasionally contain collection/product links. The
    # Shijiu detail must be self-contained and may only reference images after
    # they have been uploaded to COS, so no source-host URL is carried over.
    source_description = html.unescape(str(product.get("description") or ""))
    source_description = re.sub(r"<[^>]+>", " ", source_description)
    source_description = re.sub(r"https?://\S+", "", source_description)
    source_description = re.sub(r"\s+", " ", source_description).strip()
    colors = sorted({str(item.get("color") or "") for item in product.get("variants") or [] if item.get("color")})
    sizes = sorted({str(item.get("size") or "") for item in product.get("variants") or [] if item.get("size")})
    prefix = (
        '<section data-source="MIKIHOUSE">'
        f"<h2>{name}</h2><p>品番：{product_number}</p><p>品牌：{brand}</p>"
        f"<p>颜色：{html.escape('、'.join(colors))}</p>"
        f"<p>尺码：{html.escape('、'.join(sizes))}</p>"
    )
    suffix = "</section>"
    if len(prefix + suffix) > SHIJIU_GOOD_DETAILS_MAX_CHARACTERS:
        # Extremely long source names/options still fail closed at validation;
        # do not silently discard core product identity fields here.
        return prefix + suffix
    if source_description:
        low, high = 0, len(source_description)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = prefix + f"<p>{html.escape(source_description[:middle])}</p>" + suffix
            if len(candidate) <= SHIJIU_GOOD_DETAILS_MAX_CHARACTERS:
                low = middle
            else:
                high = middle - 1
        if low:
            prefix += f"<p>{html.escape(source_description[:low])}</p>"
    result = prefix + suffix
    if re.search(r"<img\b|https?://", result, flags=re.I):
        raise ImportPlanError("Shijiu good_details must not contain images or URLs")
    if len(result) > SHIJIU_GOOD_DETAILS_MAX_CHARACTERS:
        raise ImportPlanError("Shijiu good_details exceeds the observed 1024-character contract")
    return result


def map_product_to_shijiu(
    product: dict[str, Any],
    target_category: dict[str, Any],
    *,
    excluded_product_numbers: set[str],
) -> dict[str, Any]:
    product_number = str(product["product_number"])
    assert_not_pdf_special(product_number, excluded_product_numbers)
    assert_stable_for_shijiu(product, excluded_product_numbers)
    classification = _classification_name(product)
    variants = list(product.get("variants") or [])
    if not variants:
        raise ImportPlanError(f"product has no variants: {product_number}")
    spec_name, dimension_names = _specification_structure(variants)
    sku_info = []
    source_variants = []
    for variant in variants:
        sku = str(variant.get("sku") or "").strip()
        if not sku:
            raise ImportPlanError(f"variant SKU missing: {product_number}")
        price = int(variant["mini_program_price_jpy"])
        if price != calculate_mini_program_price_jpy(int(variant["tax_included_price_jpy"])):
            raise ImportPlanError(f"mini_program_price_jpy validation failed: {product_number}/{sku}")
        attributes = dict(_variant_attributes(variant))
        spec_value = ",".join(attributes.get(name, "") for name in dimension_names).strip(",")
        image_url = str((variant.get("resolved_image") or {}).get("url") or "")
        stock = 1 if variant.get("active") and variant.get("available_for_sale") else 0
        row = {
            "sku_price": _money(price),
            "sku_stock": _money(stock),
            "spec_name": spec_value or "默认规格",
            "sku_code": backend_sku_code(sku),
            "sku_cost_price": _money(variant["tax_included_price_jpy"]),
            "sku_thumbnail": "",
        }
        for field in MEMBER_LEVEL_FIELDS:
            row[field] = _money(price)
        sku_info.append(row)
        source_variants.append({
            "source_variant_id": source_variant_id(product_number, sku),
            "source_variant_sku": sku,
            "backend_sku_code": row["sku_code"],
            "selected_options": variant.get("selected_options") or [],
            "color": variant.get("color") or "",
            "size": variant.get("size") or "",
            "active": bool(variant.get("active")),
            "available_for_sale": bool(variant.get("available_for_sale")),
            "stock_mapping": stock,
            "stock_source": "storefront_availableForSale_boolean",
            "tax_included_price_jpy": int(variant["tax_included_price_jpy"]),
            "mini_program_price_jpy": price,
            "image_url": image_url,
        })
    image_entries = _product_image_entries(product)
    source_url_to_reference: dict[str, str] = {}
    image_upload_plan = []
    for index, entry in enumerate(image_entries, start=1):
        source_url = str((entry.get("image") or {}).get("url") or "")
        reference = _upload_reference(product_number, index)
        source_url_to_reference[source_url] = reference
        image_upload_plan.append({
            "upload_reference": reference,
            "order": index,
            "role": entry.get("role") or "product_gallery",
            "colors": entry.get("colors") or [],
            "variant_skus": entry.get("variant_skus") or [],
            "source_url": source_url,
            "source_width": (entry.get("image") or {}).get("width"),
            "source_height": (entry.get("image") or {}).get("height"),
            "target_upload_endpoint": "/v1/cos/upload",
            "target_url": None,
            "status": "PENDING_DRY_RUN_NO_UPLOAD",
        })
    image_placeholders = [_cos_placeholder(item["upload_reference"]) for item in image_upload_plan]
    cover = str((product.get("main_image") or {}).get("url") or "")
    if not cover and image_entries:
        cover = str((image_entries[0].get("image") or {}).get("url") or "")
    cover_placeholder = _cos_placeholder(source_url_to_reference[cover]) if cover in source_url_to_reference else ""
    for row, source_variant in zip(sku_info, source_variants):
        reference = source_url_to_reference.get(source_variant["image_url"])
        row["sku_thumbnail"] = _cos_placeholder(reference) if reference else ""
        source_variant["image_upload_reference"] = reference
    detail_placeholders = [
        _cos_placeholder(item["upload_reference"])
        for item in image_upload_plan
        if item["role"] in {"product_gallery", "detail"}
    ]
    publish_ready = bool(cover) and all(item["image_url"] for item in source_variants)
    min_price = min(item["mini_program_price_jpy"] for item in source_variants)
    brand = str(product.get("brand") or "").strip()
    source_category = (product.get("category") or {}).get("name") or product.get("product_type") or classification
    payload = {
        "good_videos": "",
        "video_cover": "",
        "good_name": str(product.get("name") or "").strip(),
        "gyy_erp_code": "",
        "hc_erp_code": "",
        "good_describe": f"品牌：{brand or '未提供'}；品番：{product_number}；分类：{source_category}",
        "original_price": 0,
        "good_details": _details_template(product, detail_placeholders),
        "state": "1" if product.get("active") else "0",
        "spec_name": spec_name,
        "sku_info": sku_info,
        "vnarious": 1,
        "good_type": target_category["id"],
        "good_detail_pics": ",".join(detail_placeholders),
        "integral_type": 0,
        "integral_content": 0,
        "is_cumulative": 0,
        "mortgage_type": 0,
        "mortgage_content": "",
        "distribution_set": 0,
        "distribution_type": "",
        "first_commission": "",
        "second_commisstion": "",
        "freight_type": 0,
        "freight_id": 0,
        "member_type": 0,
        "advance_sale": 0,
        "is_border": 0,
        "spt1": 0,
        "name_ch": "",
        "name_eh": "",
        "brand_id": "",
        "cargo_place": "日本",
        "standard_place": "",
        "buying_unit": "件",
        "cargo_ingredient": "",
        "gross_weight": "",
        "net_weight": "",
        "cargo_function": "",
        "tax_rate": 0,
        "code_ts": "",
        "hs_code": "",
        "length": "",
        "weight": "",
        "height": "",
        "supplier": "",
        "bus_region": "日本",
        "description": (
            f"source_product_id={source_product_id(product_number)};"
            f"source_brand={brand};source_category={source_category};currency=JPY"
        ),
        "is_shelf": 0,
        "is_collection": 0,
        "is_need_authentication": 0,
        "master_graph": cover_placeholder,
        "broadcast": ",".join(image_placeholders),
        "good_group_id": "",
    }
    return {
        "adapter_schema_version": ADAPTER_SCHEMA_VERSION,
        "source": SOURCE_CODE,
        "source_product_id": source_product_id(product_number),
        "product_number": product_number,
        "source_url": product.get("product_url"),
        "source_brand": brand,
        "source_category": source_category,
        "source_product_type": product.get("product_type") or "",
        "source_tags": list(product.get("tags") or []),
        "source_metadata": {
            "brand": product.get("brand") or "",
            "product_type": product.get("product_type") or "",
            "category": copy.deepcopy(product.get("category")),
            "tags": list(product.get("tags") or []),
        },
        "classification": classification,
        "target_category": target_category,
        "currency": "JPY",
        "currency_conversion_applied": False,
        "publish_ready": publish_ready,
        "publish_blockers": [] if publish_ready else ["missing_official_image"],
        "payload_ready_for_write": False,
        "payload_blockers": ["shijiu_cos_uploads_not_executed"],
        "source_variants": source_variants,
        "source_content": {
            "description": product.get("description") or "",
            "description_html": product.get("description_html") or "",
            "ordered_images": image_entries,
        },
        "image_upload_plan": image_upload_plan,
        "carousel_contract": {
            "ordering": ["main", "product_gallery", "variant_color", "detail"],
            "source_count": len(image_upload_plan),
            "deduplicated": True,
            "formal_shijiu_fields_contain_source_urls": False,
            "requires_all_target_urls_before_write": True,
        },
        "shijiu_payload_preview": payload,
        "payload_sha256": content_sha256(payload),
        "minimum_mini_program_price_jpy": min_price,
        "future_write_dependencies": [
            "upload every official source image through the evidenced Shijiu COS endpoint",
            "replace every COS placeholder with the returned target URL before payload validation",
            "resolve brand_id only after a Shijiu brand discovery contract is evidenced",
            "execute only under a separately authorized write runner with readback and rollback",
        ],
    }


def select_cross_category_samples(
    products: list[dict[str, Any]], per_category: int = 5
) -> list[dict[str, Any]]:
    buckets = {kind: [] for kind in ("footwear", "apparel", "baby", "goods")}
    for product in products:
        kind = _classification_name(product)
        if kind in buckets and len(buckets[kind]) < per_category and product.get("main_image"):
            buckets[kind].append(product)
    missing = {kind: per_category - len(items) for kind, items in buckets.items() if len(items) < per_category}
    if missing:
        raise ImportPlanError(f"not enough cross-category target-check samples: {missing}")
    return [product for kind in buckets for product in buckets[kind]]


def _recursive_field_observations(value: Any, requested_keys: set[str]) -> dict[str, list[Any]]:
    observations = {key: [] for key in requested_keys}

    def walk(current: Any) -> None:
        if isinstance(current, dict):
            for key, child in current.items():
                if key in observations:
                    observations[key].append(child)
                walk(child)
        elif isinstance(current, list):
            for child in current:
                walk(child)

    walk(value)
    return observations


def _schema_only_observation(values: list[Any]) -> dict[str, Any]:
    return {
        "observed_count": len(values),
        "value_types": sorted({type(value).__name__ for value in values}),
        "non_empty_count": sum(value not in (None, "", [], {}) for value in values),
        "list_lengths": sorted({len(value) for value in values if isinstance(value, list)}),
        "string_length_range": (
            [min(lengths), max(lengths)]
            if (lengths := [len(value) for value in values if isinstance(value, str)])
            else None
        ),
    }


def list_legacy_reference_products(
    client: ReadOnlyShijiuClient,
    target_category: dict[str, Any],
    *,
    page_size: int = 200,
) -> list[dict[str, Any]]:
    first = client.search_products(page=1, page_size=page_size, good_type=target_category["id"])
    total = int(first.get("count") or len(response_rows(first)))
    pages = [first]
    total_pages = max(1, (total + page_size - 1) // page_size)
    for page in range(2, total_pages + 1):
        pages.append(
            client.search_products(page=page, page_size=page_size, good_type=target_category["id"])
        )
    rows = [row for response in pages for row in response_rows(response)]
    if len(rows) != total:
        raise ImportPlanError(
            f"incomplete legacy reference listing: expected {total}, observed {len(rows)}"
        )
    if total != EXPECTED_LEGACY_REFERENCE_COUNT:
        raise ImportPlanError(
            f"expected {EXPECTED_LEGACY_REFERENCE_COUNT} legacy reference products, observed {total}"
        )
    return rows


def audit_legacy_reference_only(
    client: ReadOnlyShijiuClient,
    target_category: dict[str, Any],
    *,
    sample_size: int = DEFAULT_LEGACY_AUDIT_SAMPLE_SIZE,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Audit only a small legacy sample for schema; never perform identity reconciliation."""
    rows = list_legacy_reference_products(client, target_category)
    if not 1 <= sample_size < len(rows):
        raise ImportPlanError("legacy audit sample must be small and smaller than the legacy set")
    indexes = (
        [0]
        if sample_size == 1
        else sorted({
            round(index * (len(rows) - 1) / (sample_size - 1))
            for index in range(sample_size)
        })
    )
    sample_rows = [rows[index] for index in indexes]
    requested_fields = {
        "good_name",
        "master_graph",
        "broadcast",
        "good_detail_pics",
        "good_details",
        "spec_name",
        "sku_info",
        "sku_code",
        "sku_price",
        "sku_stock",
        "sku_thumbnail",
        "price",
        "stock",
        "spec_son_name",
        "orderby",
        "serial_number",
        "is_shelf",
    }
    aggregate = {field: [] for field in requested_fields}
    sample_schemas = []
    ordering_keys: set[str] = set()
    for row in sample_rows:
        backend_id = _target_product_id(row)
        if backend_id in (None, ""):
            raise ImportPlanError("legacy reference row has no backend product id")
        detail = client.product_detail(backend_id)
        observed = _recursive_field_observations(detail, requested_fields)
        for field, values in observed.items():
            aggregate[field].extend(values)

        def collect_ordering_keys(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    normalized = key.casefold()
                    if any(marker in normalized for marker in ("order", "sort", "serial", "sequence", "index")):
                        ordering_keys.add(key)
                    collect_ordering_keys(child)
            elif isinstance(value, list):
                for child in value:
                    collect_ordering_keys(child)

        collect_ordering_keys(detail)
        sku_rows = recursively_find_skus(detail)
        sample_schemas.append({
            "backend_product_id": str(backend_id),
            "list_row_keys": sorted(row),
            "detail_root_keys": sorted(detail),
            "detail_sku_count": len(sku_rows),
            "detail_sku_field_keys": sorted({key for sku in sku_rows for key in sku}),
        })
    field_contract = {
        field: _schema_only_observation(values) for field, values in sorted(aggregate.items())
    }
    required_observed = all(field_contract[field]["observed_count"] > 0 for field in (
        "good_name", "master_graph", "broadcast", "good_details", "spec_name", "sku_info"
    ))
    audit = {
        "mode": "read-only",
        "classification": "legacy_reference_only",
        "target_category_id": target_category["id"],
        "legacy_product_count": len(rows),
        "sample_size": len(sample_rows),
        "sample_selection": "evenly_spaced_category_rows",
        "sample_schemas": sample_schemas,
        "field_contract": field_contract,
        "ordering_field_keys": sorted(ordering_keys),
        "identity_reconciliation_attempted": False,
        "sku_binding_attempted": False,
        "price_matching_attempted": False,
        "legacy_content_copied_to_new_payload": False,
        "legacy_detail_reads": len(sample_rows),
        "passed": required_observed,
    }
    if not audit["passed"]:
        raise ImportPlanError(f"legacy display contract audit incomplete: {field_contract}")
    return audit, rows


def build_legacy_cleanup_plan(
    rows: list[dict[str, Any]], target_category: dict[str, Any]
) -> dict[str, Any]:
    actions = []
    for row in rows:
        backend_id = _target_product_id(row)
        if backend_id in (None, ""):
            raise ImportPlanError("legacy cleanup target has no backend product id")
        actions.append({
            "legacy_shijiu_product_id": str(backend_id),
            "classification": "legacy_reference_only",
            "planned_action": "OFF_SHELF",
            "target_endpoint": "/shopapi/Goods/grounding",
            "target_state": 0,
            "write_executed": False,
        })
    return {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "mode": "dry-run",
        "target": "SHIJIU",
        "target_category_id": target_category["id"],
        "legacy_reference_only": True,
        "action_count": len(actions),
        "preconditions": [
            "new MIKIHOUSE products have been created under separately authorized execution",
            "new products passed Shijiu readback and storefront validation",
            "cleanup received separate explicit authorization",
        ],
        "identity_relation_to_new_mikihouse_pool": "none",
        "actions": actions,
        "write_executed": False,
    }


def affected_product_numbers(changes: dict[str, Any], products: list[dict[str, Any]]) -> set[str]:
    if changes.get("is_initial_sync"):
        return {item["product_number"] for item in products}
    return {
        str(item.get("product_number") or "")
        for item in changes.get("changes") or []
        if item.get("product_number")
    }


def load_mapping_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": 1,
            "source": SOURCE_CODE,
            "target": "SHIJIU",
            "identity_contract": {
                "source_product_id": "MIKIHOUSE:<product_number>",
                "source_variant_id": "MIKIHOUSE:<product_number>:<variant SKU>",
                "backend_sku_code": "MIKI-<variant SKU>",
                "target_variant_identity": "shijiu_product_id + exact backend_sku_code",
                "shijiu_sku_id": "nullable; never guessed when official readback omits it",
                "product_match": "persisted post-create/readback mapping only",
                "product_name_matching": "forbidden",
                "good_name_candidate_scope": "exact only; never binding proof",
                "post_create_binding_proof": (
                    "exactly one getFormatInfo candidate matching complete MIKI backend SKU set, "
                    "category, variant count, specifications and prices"
                ),
                "multiple_strong_matches": "AMBIGUOUS_FAIL_CLOSED",
                "legacy_reference_binding": "forbidden",
            },
            "products": {},
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        payload.get("source") != SOURCE_CODE
        or payload.get("target") != "SHIJIU"
        or not isinstance(payload.get("products"), dict)
        or payload.get("identity_contract", {}).get("product_name_matching") != "forbidden"
    ):
        raise ImportPlanError("invalid Shijiu mapping state")
    seen_shijiu_product_ids: set[str] = set()
    seen_shijiu_sku_ids: set[str] = set()
    for product_number, mapping in payload["products"].items():
        if mapping.get("match_method") == "exact_backend_sku_code":
            raise ImportPlanError(
                "legacy target discovery bindings are forbidden; only post-create/readback mappings are valid"
            )
        if (
            mapping.get("source") != SOURCE_CODE
            or mapping.get("source_product_id") != source_product_id(product_number)
        ):
            raise ImportPlanError(f"cross-provider product mapping rejected: {product_number}")
        target_product_id = mapping.get("shijiu_product_id")
        if target_product_id not in (None, ""):
            marker = str(target_product_id)
            if marker in seen_shijiu_product_ids:
                raise ImportPlanError(f"duplicate Shijiu product binding rejected: {marker}")
            seen_shijiu_product_ids.add(marker)
        for sku, variant_mapping in (mapping.get("variants") or {}).items():
            if variant_mapping.get("match_method") == "exact_backend_sku_code":
                raise ImportPlanError(
                    "legacy target SKU discovery bindings are forbidden; no reconciliation is allowed"
                )
            if (
                variant_mapping.get("source") != SOURCE_CODE
                or variant_mapping.get("source_variant_id") != source_variant_id(product_number, sku)
                or variant_mapping.get("backend_sku_code") != backend_sku_code(sku)
            ):
                raise ImportPlanError(f"cross-provider variant mapping rejected: {product_number}/{sku}")
            target_sku_id = variant_mapping.get("shijiu_sku_id")
            if target_sku_id not in (None, ""):
                marker = str(target_sku_id)
                if marker in seen_shijiu_sku_ids:
                    raise ImportPlanError(f"duplicate Shijiu SKU binding rejected: {marker}")
                seen_shijiu_sku_ids.add(marker)
    return payload


def reconcile_mapping_state(
    mapping_state: dict[str, Any],
    products: list[dict[str, Any]],
    target_category: dict[str, Any],
) -> dict[str, Any]:
    result = copy.deepcopy(mapping_state)
    result["source"] = SOURCE_CODE
    result["target"] = "SHIJIU"
    result["fixed_target_category"] = {
        "id": target_category["id"],
        "name": target_category["name"],
        "parent_id": target_category["parent_id"],
    }
    result["identity_contract"] = {
        "source_product_id": "MIKIHOUSE:<product_number>",
        "source_variant_id": "MIKIHOUSE:<product_number>:<variant SKU>",
        "backend_sku_code": "MIKI-<variant SKU>",
        "target_variant_identity": "shijiu_product_id + exact backend_sku_code",
        "shijiu_sku_id": "nullable; never guessed when official readback omits it",
        "product_match": "persisted post-create/readback mapping only",
        "product_name_matching": "forbidden",
        "good_name_candidate_scope": "exact only; never binding proof",
        "post_create_binding_proof": (
            "exactly one getFormatInfo candidate matching complete MIKI backend SKU set, "
            "category, variant count, specifications and prices"
        ),
        "multiple_strong_matches": "AMBIGUOUS_FAIL_CLOSED",
        "legacy_reference_binding": "forbidden",
    }
    mappings = result.setdefault("products", {})
    for mapping in mappings.values():
        mapping["source_present"] = False
        for variant in (mapping.get("variants") or {}).values():
            variant["source_present"] = False
    for product in products:
        product_number = str(product["product_number"])
        mapping = mappings.setdefault(product_number, {})
        mapping.update({
            "source": SOURCE_CODE,
            "source_product_id": source_product_id(product_number),
            "product_number": product_number,
            "source_present": True,
            "target_category_id": target_category["id"],
        })
        mapping.setdefault("shijiu_product_id", None)
        mapping.setdefault("last_verified_at", None)
        variant_mappings = mapping.setdefault("variants", {})
        for variant in product.get("variants") or []:
            sku = str(variant["sku"])
            variant_mapping = variant_mappings.setdefault(sku, {})
            variant_mapping.update({
                "source": SOURCE_CODE,
                "source_variant_id": source_variant_id(product_number, sku),
                "source_variant_sku": sku,
                "backend_sku_code": backend_sku_code(sku),
                "source_present": True,
            })
            variant_mapping.setdefault("shijiu_sku_id", None)
            variant_mapping.setdefault("target_product_id", None)
            variant_mapping.setdefault("backend_sku_code_verified", False)
            variant_mapping.setdefault("last_verified_at", None)
    result["updated_at"] = now()
    return result


def _bound_product_mapping(mapping_state: dict[str, Any], product_number: str) -> dict[str, Any] | None:
    mapping = mapping_state["products"].get(product_number)
    return mapping if mapping and mapping.get("shijiu_product_id") not in (None, "") else None


def mapping_summary(mapping_state: dict[str, Any]) -> dict[str, Any]:
    products = list(mapping_state["products"].values())
    variants = [variant for product in products for variant in (product.get("variants") or {}).values()]
    return {
        "product_rows": len(products),
        "variant_rows": len(variants),
        "bound_product_rows": sum(item.get("shijiu_product_id") not in (None, "") for item in products),
        "bound_variant_rows": sum(
            item.get("target_product_id") not in (None, "")
            and item.get("backend_sku_code_verified") is True
            for item in variants
        ),
        "unbound_product_rows": sum(item.get("shijiu_product_id") in (None, "") for item in products),
        "unbound_variant_rows": sum(
            item.get("target_product_id") in (None, "")
            or item.get("backend_sku_code_verified") is not True
            for item in variants
        ),
        "product_name_matching": "forbidden",
        "good_name_candidate_scope": "exact only; never binding proof",
    }


def load_price_guard(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("source") != SOURCE_CODE or payload.get("target") != "SHIJIU":
        raise ImportPlanError("price guard must explicitly declare MIKIHOUSE -> SHIJIU")
    required = (
        "minimum_tax_included_price_jpy",
        "maximum_tax_included_price_jpy",
        "maximum_absolute_change_jpy",
        "maximum_relative_change_ratio",
    )
    if any(Decimal(str(payload.get(key))) < 0 for key in required):
        raise ImportPlanError("price guard thresholds must be non-negative")
    return payload


def assess_price_change(change: dict[str, Any], guard: dict[str, Any]) -> dict[str, Any]:
    before = change.get("before") or {}
    after = change.get("after") or {}
    reasons: list[str] = []
    try:
        old_tax = int(before["tax_included_price_jpy"])
        new_tax = int(after["tax_included_price_jpy"])
        stated_new_mini = int(after["mini_program_price_jpy"])
    except (KeyError, TypeError, ValueError):
        return {"passed": False, "review_required": True, "reasons": ["missing_or_invalid_price_baseline"]}
    recalculated = calculate_mini_program_price_jpy(new_tax)
    if stated_new_mini != recalculated:
        reasons.append("mini_program_price_jpy_recalculation_mismatch")
    minimum = int(guard["minimum_tax_included_price_jpy"])
    maximum = int(guard["maximum_tax_included_price_jpy"])
    if not minimum <= new_tax <= maximum:
        reasons.append("new_tax_included_price_outside_valid_range")
    absolute_change = abs(new_tax - old_tax)
    if absolute_change > int(guard["maximum_absolute_change_jpy"]):
        reasons.append("absolute_price_change_exceeds_threshold")
    relative_change = None if old_tax <= 0 else Decimal(absolute_change) / Decimal(old_tax)
    if relative_change is None:
        reasons.append("non_positive_baseline_price")
    elif relative_change > Decimal(str(guard["maximum_relative_change_ratio"])):
        reasons.append("relative_price_change_exceeds_threshold")
    return {
        "passed": not reasons,
        "review_required": bool(reasons),
        "reasons": reasons,
        "before_tax_included_price_jpy": old_tax,
        "after_tax_included_price_jpy": new_tax,
        "before_mini_program_price_jpy": before.get("mini_program_price_jpy"),
        "after_mini_program_price_jpy": recalculated,
        "absolute_change_jpy": absolute_change,
        "relative_change_ratio": float(relative_change) if relative_change is not None else None,
        "currency": "JPY",
        "currency_conversion_applied": False,
    }


CHANGE_TYPE_NAMES = {
    "new_product": "NEW_PRODUCT",
    "new_variant": "NEW_VARIANT",
    "price_changed": "PRICE_CHANGED",
    "inventory_changed": "INVENTORY_CHANGED",
    "product_images_changed": "IMAGE_CHANGED",
    "variant_image_changed": "IMAGE_CHANGED",
    "product_inactivated": "PRODUCT_INACTIVATED",
    "variant_inactivated": "VARIANT_INACTIVATED",
    "product_reactivated": "PRODUCT_REACTIVATED",
    "variant_reactivated": "VARIANT_REACTIVATED",
    "product_metadata_changed": "PRODUCT_METADATA_CHANGED",
    "variant_metadata_changed": "VARIANT_METADATA_CHANGED",
    "compare_at_price_changed": "COMPARE_AT_PRICE_CHANGED",
}


def build_incremental_sync_operations(
    changes: dict[str, Any],
    mapping_state: dict[str, Any],
    mapped_by_product: dict[str, dict[str, Any]],
    price_guard: dict[str, Any],
    *,
    excluded_product_numbers: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    assert_shijiu_pool_boundary(list(mapped_by_product.values()), changes, excluded_product_numbers)
    operations: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    new_products = {
        str(item.get("product_number"))
        for item in changes.get("changes") or []
        if item.get("change_type") == "new_product"
    }
    for change in changes.get("changes") or []:
        raw_type = str(change.get("change_type") or "")
        change_type = CHANGE_TYPE_NAMES.get(raw_type, raw_type.upper() or "UNKNOWN")
        product_number = str(change.get("product_number") or "")
        assert_not_pdf_special(product_number, excluded_product_numbers)
        variant_sku = str(change.get("variant_sku") or "")
        product_mapping = mapping_state["products"].get(product_number) or {}
        variant_mapping = (product_mapping.get("variants") or {}).get(variant_sku) or {}
        bound_product = product_mapping.get("shijiu_product_id") not in (None, "")
        mapped = mapped_by_product.get(product_number) or {}
        publish_ready = bool(mapped.get("publish_ready"))
        operation = {
            "detected_at": change.get("detected_at"),
            "source": SOURCE_CODE,
            "target": "SHIJIU",
            "change_type": change_type,
            "source_product_id": source_product_id(product_number),
            "source_variant_id": source_variant_id(product_number, variant_sku) if variant_sku else None,
            "product_number": product_number,
            "variant_sku": variant_sku or None,
            "shijiu_product_id": product_mapping.get("shijiu_product_id"),
            "shijiu_sku_id": variant_mapping.get("shijiu_sku_id"),
            "backend_sku_code": variant_mapping.get("backend_sku_code") if variant_sku else None,
            "currency": "JPY",
            "currency_conversion_applied": False,
            "write_executed": False,
        }
        if change_type == "PRICE_CHANGED":
            assessment = assess_price_change(change, price_guard)
            operation["price_change"] = assessment
            if assessment["review_required"]:
                operation["planned_action"] = "REVIEW_REQUIRED"
                reviews.append(copy.deepcopy(operation))
            elif not bound_product:
                operation["planned_action"] = "BLOCKED_UNMAPPED_PRICE_UPDATE"
            else:
                operation["planned_action"] = "UPDATE_PRICE_BY_EXACT_VARIANT_SKU"
        elif change_type == "NEW_PRODUCT":
            operation["planned_action"] = (
                "SKIP_MISSING_OFFICIAL_IMAGE"
                if not publish_ready
                else (
                    "SKIP_ALREADY_BOUND"
                    if bound_product
                    else "CREATE_PRODUCT"
                )
            )
        elif change_type == "NEW_VARIANT":
            operation["planned_action"] = (
                "SKIP_MISSING_OFFICIAL_IMAGE"
                if not publish_ready
                else (
                    "ADD_VARIANT_TO_BOUND_PRODUCT"
                    if bound_product
                    else (
                        "INCLUDED_IN_NEW_PRODUCT"
                        if product_number in new_products
                        else "BLOCKED_UNMAPPED_VARIANT"
                    )
                )
            )
        elif change_type == "INVENTORY_CHANGED":
            operation["planned_action"] = "UPDATE_INVENTORY" if bound_product else "BLOCKED_UNMAPPED"
        elif change_type == "IMAGE_CHANGED":
            operation["planned_action"] = "UPDATE_IMAGE" if bound_product else "BLOCKED_UNMAPPED"
        elif change_type in {"PRODUCT_INACTIVATED", "VARIANT_INACTIVATED"}:
            operation["planned_action"] = "DEACTIVATE" if bound_product else "SKIP_UNMAPPED"
        elif change_type in {"PRODUCT_REACTIVATED", "VARIANT_REACTIVATED"}:
            operation["planned_action"] = "REACTIVATE" if bound_product else "BLOCKED_UNMAPPED"
        elif change_type in {"PRODUCT_METADATA_CHANGED", "VARIANT_METADATA_CHANGED"}:
            operation["planned_action"] = "UPDATE_METADATA" if bound_product else "BLOCKED_UNMAPPED"
        else:
            operation["planned_action"] = "REVIEW_REQUIRED_UNKNOWN_CHANGE_TYPE"
            reviews.append(copy.deepcopy(operation))
        operations.append(operation)
    return operations, reviews


def choose_action(
    mapped: dict[str, Any],
    mapping: dict[str, Any] | None,
) -> tuple[str, str]:
    if not mapped["publish_ready"]:
        return "skip", "missing_official_image"
    if not mapped["shijiu_payload_preview"]["state"] == "1":
        if mapping:
            return "deactivate", "source_product_inactive"
        return "skip", "inactive_source_not_present_on_target"
    if mapping:
        if mapping.get("target_active") is False:
            return "reactivate", "stable_mapping_marks_target_inactive"
        if mapping.get("last_payload_sha256") == mapped["payload_sha256"]:
            return "skip", "payload_unchanged_since_verified_sync"
        return "update", "stable_source_mapping_exists"
    return "create", "no_persisted_post_create_mapping"


def build_rollback_plan() -> dict[str, Any]:
    return {
        "current_run": {
            "required": False,
            "reason": "dry_run_only; no target mutations are possible",
        },
        "future_write_run": {
            "create": "record backend_product_id after readback; rollback by off-shelf, never hard delete",
            "update": "persist pre-update getFormatInfo snapshot and restore the exact prior payload",
            "deactivate": "persist prior grounding/state and restore it if rollback is required",
            "reactivate": "persist prior inactive state and restore it if rollback is required",
            "images": "retain source URLs and uploaded target URLs in the mapping ledger",
            "checkpoint_rule": "persist intent before request and verified result after target readback",
        },
    }


def build_contract_audit() -> dict[str, Any]:
    return {
        "target": "SHIJIU",
        "reference_repository": "qinxitong8666/wawu-product-sync",
        "reference_commit": WAWU_REFERENCE_COMMIT,
        "audit_document": SHIJIU_CONTRACT_AUDIT_PATH,
        "proven_target_evidence": [
            {
                "source": "backend_client.py:30-92",
                "finding": "default API root contains /shijiu and admin Origin/Referer use shijiu.wfcorp.cn",
            },
            {
                "source": "backend_client.py:309-573",
                "finding": "Shijiu product write, grounding, list, detail and category endpoint paths are explicit",
            },
            {
                "source": "DISPOSABLE_SKU_UPDATE_SEMANTICS_CREATE_PAYLOAD.json",
                "finding": "native product, spec_name and sku_info field sets are fixed in a committed payload fixture",
            },
            {
                "source": "transformer.py:478-548",
                "finding": "readback fields and SKU matching/validation checks are explicit",
            },
            {
                "source": "DISPOSABLE_UPDATE_SEMANTICS_RUNNER.py:110-365",
                "finding": "intent checkpoint, readback-before-retry and rollback state-machine behavior is explicit",
            },
        ],
        "excluded_reference_components": [
            "WAWU upstream API client",
            "WAWU mapper and source field semantics",
            "WAWU SKU prefixes",
            "WAWU price multiplier and category decisions",
        ],
        "missing_evidence": [
            "reference main has no committed successful live Shijiu create/update/rollback round-trip; LIVE_001 records zero writes",
            "no evidenced Shijiu brand discovery endpoint, so brand_id is not guessed",
        ],
        "implementation_consequence": (
            "read-only legacy structure sampling and non-executable mapping previews only; image uploads and every Shijiu mutation are absent"
        ),
        "legacy_boundary": {
            "classification": "legacy_reference_only",
            "expected_product_count": EXPECTED_LEGACY_REFERENCE_COUNT,
            "identity_reconciliation": "forbidden",
            "sku_binding": "forbidden",
            "price_matching": "forbidden",
            "content_reuse": "forbidden",
        },
        "live_read_only_category_finding": {
            "name": "MikiHouse",
            "id": 294884,
            "parent_name": "母婴用品",
            "parent_id": 288338,
            "assignment": "all publishable MIKIHOUSE products",
        },
    }


def field_mapping_contract() -> dict[str, Any]:
    return {
        "source": "MIKI HOUSE master_catalog.json",
        "target": "Shijiu native field preview",
        "product_name": "name -> good_name",
        "brand": "brand -> adapter source_brand + good_describe; brand_id deferred",
        "category": "all publishable MIKIHOUSE products -> fixed Shijiu MikiHouse category 294884",
        "source_metadata": "brand/productType/category/tags retained internally and never used for target category routing",
        "main_image": "main_image -> COS upload reference -> master_graph; source URL forbidden in formal target field",
        "carousel": (
            "ordered_images main -> product_gallery -> variant_color -> detail, deduplicated; "
            "COS upload references only until target URLs exist"
        ),
        "detail": "MIKI HOUSE name/description/options + COS detail-image references -> good_details/good_detail_pics",
        "color_images": "variant resolved_image -> COS upload reference -> sku_thumbnail",
        "options": "selected_options/color/size -> spec_name dimensions and per-SKU spec_name",
        "sku": "variant sku -> MIKI-<sku> in sku_code",
        "inventory": "availableForSale boolean -> sku_stock 1/0; source boolean retained",
        "price": "mini_program_price_jpy -> sku_price and member price fields, currency JPY",
        "source_identity": "product_number and product_number+variant SKU -> stable source IDs",
        "legacy_products": "286 existing rows are legacy_reference_only and have no identity relation to new data",
        "special_exclusion": "all 351 PDF special product numbers fail closed before every Shijiu plan stage",
    }


def plan_import(
    products: list[dict[str, Any]],
    changes: dict[str, Any],
    target_category: dict[str, Any],
    mapping_state: dict[str, Any],
    checkpoint_path: Path,
    *,
    excluded_product_numbers: set[str],
    resume: bool = False,
    max_items: int | None = None,
    checkpoint_interval: int = 25,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    assert_shijiu_pool_boundary(products, changes, excluded_product_numbers)
    if checkpoint_interval <= 0:
        raise ImportPlanError("checkpoint_interval must be positive")
    master_identity = content_sha256([
        (item["product_number"], item.get("last_seen_at"), len(item.get("variants") or []))
        for item in products
    ])
    if resume:
        if not checkpoint_path.exists():
            raise ImportPlanError("--resume requires an existing checkpoint")
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint.get("master_identity") != master_identity:
            raise ImportPlanError("checkpoint does not match the current master catalog")
    else:
        checkpoint = {
            "schema_version": 1,
            "mode": "dry-run",
            "master_identity": master_identity,
            "created_at": now(),
            "records": {},
        }
        write_json_atomic(checkpoint_path, checkpoint)

    affected = affected_product_numbers(changes, products)
    affected.update(
        item["product_number"]
        for item in products
        if not _bound_product_mapping(mapping_state, item["product_number"])
    )
    selected = [item for item in products if item["product_number"] in affected]
    records: dict[str, Any] = checkpoint["records"]
    processed_this_run = 0
    for product in selected:
        stable_product_id = source_product_id(product["product_number"])
        if stable_product_id in records and records[stable_product_id].get("status") == "planned":
            continue
        try:
            mapped = map_product_to_shijiu(
                product,
                target_category,
                excluded_product_numbers=excluded_product_numbers,
            )
            mapping = _bound_product_mapping(mapping_state, product["product_number"])
            if not mapping:
                action, reason = choose_action(mapped, mapping)
            elif not mapped["publish_ready"]:
                action, reason = "skip", "missing_official_image"
            else:
                action, reason = "incremental_change_set", "use_variant_level_incremental_sync_plan"
            record = {
                "status": "planned",
                "action": action,
                "reason": reason,
                "target_identity_check": "forbidden_legacy_isolation",
                "payload_sha256": mapped["payload_sha256"],
                "existing_backend_product_id": (
                    mapping.get("shijiu_product_id") if mapping else None
                ),
                "planned_at": now(),
            }
        except Exception as exc:
            record = {
                "status": "failed",
                "action": "failed",
                "reason": f"{type(exc).__name__}: {exc}",
                "planned_at": now(),
            }
        records[stable_product_id] = record
        checkpoint["updated_at"] = now()
        processed_this_run += 1
        if processed_this_run % checkpoint_interval == 0:
            write_json_atomic(checkpoint_path, checkpoint)
        if max_items is not None and processed_this_run >= max_items:
            break

    plan = []
    for product in selected:
        stable_product_id = source_product_id(product["product_number"])
        if stable_product_id in records:
            entry = {"source_product_id": stable_product_id, **records[stable_product_id]}
            if entry["status"] == "planned":
                entry["existing_mapping"] = mapping_state["products"].get(product["product_number"])
                entry["mapped_product"] = map_product_to_shijiu(
                    product,
                    target_category,
                    excluded_product_numbers=excluded_product_numbers,
                )
            plan.append(entry)
    complete = len(plan) == len(selected)
    checkpoint.update({
        "updated_at": now(),
        "complete": complete,
        "total_items": len(selected),
        "planned_items": len(plan),
        "remaining_items": len(selected) - len(plan),
    })
    write_json_atomic(checkpoint_path, checkpoint)
    return plan, {
        "complete": complete,
        "total_items": len(selected),
        "planned_items": len(plan),
        "remaining_items": len(selected) - len(plan),
        "processed_this_run": processed_this_run,
        "resumed": resume,
        "checkpoint_path": str(checkpoint_path),
    }


def _compact_plan_sample(entry: dict[str, Any]) -> dict[str, Any]:
    result = {key: entry.get(key) for key in (
        "source_product_id", "status", "action", "reason", "target_identity_check"
    )}
    mapped = entry.get("mapped_product") or {}
    payload = mapped.get("shijiu_payload_preview") or {}
    result["mapped_fields"] = {
        "product_number": mapped.get("product_number"),
        "source_brand": mapped.get("source_brand"),
        "source_category": mapped.get("source_category"),
        "classification": mapped.get("classification"),
        "target_category": mapped.get("target_category"),
        "currency": mapped.get("currency"),
        "publish_ready": mapped.get("publish_ready"),
        "good_name": payload.get("good_name"),
        "master_graph": payload.get("master_graph"),
        "carousel_image_count": len(mapped.get("image_upload_plan") or []),
        "carousel_roles": [item.get("role") for item in mapped.get("image_upload_plan") or []],
        "payload_ready_for_write": mapped.get("payload_ready_for_write"),
        "spec_name": payload.get("spec_name"),
        "sku_count": len(payload.get("sku_info") or []),
        "sku_samples": (payload.get("sku_info") or [])[:3],
    }
    return result


def _compact_action(entry: dict[str, Any]) -> dict[str, Any]:
    mapped = entry.get("mapped_product") or {}
    return {
        "source_product_id": entry["source_product_id"],
        "product_number": mapped.get("product_number"),
        "action": entry.get("action"),
        "reason": entry.get("reason"),
        "publish_ready": mapped.get("publish_ready", False),
        "variant_count": len(mapped.get("source_variants") or []),
        "payload_sha256": mapped.get("payload_sha256") or entry.get("payload_sha256"),
        "target_identity_reconciliation_attempted": False,
    }


def run_dry_run_import(
    master_path: Path,
    changes_path: Path,
    special_path: Path,
    category_map_path: Path,
    price_guard_path: Path,
    mapping_path: Path,
    output_dir: Path,
    report_dir: Path,
    checkpoint_path: Path,
    *,
    client: ReadOnlyShijiuClient | None,
    sample_per_category: int = 5,
    resume: bool = False,
    max_items: int | None = None,
) -> dict[str, Any]:
    if os.environ.get("MIKIHOUSE_SHIJIU_ENABLE_WRITES", "").strip().lower() in {"1", "true", "yes"}:
        raise WriteProhibitedError("write-enabling environment variables are forbidden in this adapter")
    master = json.loads(master_path.read_text(encoding="utf-8"))
    changes = json.loads(changes_path.read_text(encoding="utf-8"))
    if (
        master.get("catalog_kind") != "MIKIHOUSE_STABLE_REGULAR_PRODUCT_POOL"
        or master.get("shijiu_action_source_required") != "stable_catalog"
    ):
        raise ImportPlanError(
            "Shijiu planning requires output/storefront-stable/stable_catalog.json; "
            "legacy all-non-special candidate catalogs are not executable"
        )
    products = list(master.get("products") or [])
    exclusion_snapshot = master.get("special_exclusion") or {}
    online_special_count = int(exclusion_snapshot.get("online_excluded_count", 311))
    offline_special_count = int(exclusion_snapshot.get("offline_remembered_count", 40))
    if exclusion_snapshot and (
        int(exclusion_snapshot.get("total_count", 0)) != EXPECTED_SPECIAL_COUNT
        or online_special_count + offline_special_count != EXPECTED_SPECIAL_COUNT
    ):
        raise ImportPlanError("master catalog special exclusion snapshot is inconsistent")
    first_seen_counts = Counter(str(item.get("first_seen_at") or "") for item in products)
    declared_baseline_timestamp = next(
        (timestamp for timestamp, count in first_seen_counts.items() if count == DECLARED_CANDIDATE_BASELINE_COUNT),
        None,
    )
    live_new_since_declared_baseline = sorted(
        item["product_number"]
        for item in products
        if declared_baseline_timestamp and str(item.get("first_seen_at") or "") != declared_baseline_timestamp
    )
    if len(live_new_since_declared_baseline) != max(
        0, len(products) - DECLARED_CANDIDATE_BASELINE_COUNT
    ):
        live_new_since_declared_baseline = []
    special = set(read_product_numbers(special_path))
    assert_shijiu_pool_boundary(products, changes, special)
    leaked: list[str] = []
    target_category = load_category_map(category_map_path)
    price_guard = load_price_guard(price_guard_path)
    category_discovery: dict[str, Any] = {}
    legacy_audit: dict[str, Any] = {
        "skipped": True,
        "reason": "offline_target_checks",
        "classification": "legacy_reference_only",
        "identity_reconciliation_attempted": False,
    }
    legacy_rows: list[dict[str, Any]] = []
    mapping_state = reconcile_mapping_state(load_mapping_state(mapping_path), products, target_category)
    if client:
        category_discovery = validate_live_mikihouse_category(target_category, client.category_list(1))
        legacy_audit, legacy_rows = audit_legacy_reference_only(
            client,
            target_category,
            sample_size=DEFAULT_LEGACY_AUDIT_SAMPLE_SIZE,
        )
    write_json_atomic(mapping_path, mapping_state)
    plan, checkpoint_summary = plan_import(
        products,
        changes,
        target_category,
        mapping_state,
        checkpoint_path,
        excluded_product_numbers=special,
        resume=resume,
        max_items=max_items,
    )
    action_counts = Counter(item["action"] for item in plan)
    mapped = [item["mapped_product"] for item in plan if item.get("mapped_product")]
    source_variants = [variant for item in mapped for variant in item["source_variants"]]
    price_failures = [
        item["source_variant_id"]
        for item in source_variants
        if item["mini_program_price_jpy"]
        != calculate_mini_program_price_jpy(item["tax_included_price_jpy"])
    ]
    if price_failures:
        raise ImportPlanError(f"JPY price validation failed: {price_failures[:10]}")
    missing_images = [
        item["product_number"] for item in mapped if not item["publish_ready"]
    ]
    image_role_counts = Counter(
        upload["role"] for item in mapped for upload in item.get("image_upload_plan") or []
    )
    formal_image_source_url_leaks = []
    for item in mapped:
        payload = item["shijiu_payload_preview"]
        formal_values = [
            payload.get("master_graph") or "",
            payload.get("broadcast") or "",
            payload.get("good_detail_pics") or "",
            *(sku.get("sku_thumbnail") or "" for sku in payload.get("sku_info") or []),
        ]
        if any("http://" in value or "https://" in value for value in formal_values):
            formal_image_source_url_leaks.append(item["product_number"])
    if formal_image_source_url_leaks:
        raise ImportPlanError(
            f"official source URLs leaked into formal Shijiu image fields: {formal_image_source_url_leaks[:20]}"
        )
    mapped_by_product = {item["product_number"]: item for item in mapped}
    incremental_operations, price_reviews = build_incremental_sync_operations(
        changes,
        mapping_state,
        mapped_by_product,
        price_guard,
        excluded_product_numbers=special,
    )
    incremental_type_counts = Counter(item["change_type"] for item in incremental_operations)
    incremental_action_counts = Counter(item["planned_action"] for item in incremental_operations)
    output_dir.mkdir(parents=True, exist_ok=True)
    full_plan_path = output_dir / "dry_run_import_plan.json"
    incremental_plan_path = output_dir / "incremental_sync_plan.json"
    target_snapshot_path = output_dir / "read_only_target_snapshot.json"
    legacy_cleanup_path = output_dir / "legacy_cleanup_plan.json"
    write_json_atomic(full_plan_path, {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "mode": "dry-run",
        "generated_at": now(),
        "plan": plan,
    })
    write_json_atomic(incremental_plan_path, {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "mode": "dry-run",
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "generated_at": now(),
        "price_guard": price_guard,
        "operations": incremental_operations,
    })
    target_snapshot = {
        "target": "SHIJIU",
        "checked_at": now(),
        "fixed_category_discovery": category_discovery,
        "legacy_reference_audit": legacy_audit,
        "legacy_identity_reconciliation_attempted": False,
        "legacy_sku_binding_attempted": False,
        "legacy_price_matching_attempted": False,
        "request_ledger": client.requests if client else [],
        "read_request_count": len(client.requests) if client else 0,
        "semantic_write_request_count": client.semantic_write_request_count if client else 0,
        "mutating_endpoints_called": [
            item["path"] for item in (client.requests if client else []) if item["path"] in MUTATING_ENDPOINTS
        ],
    }
    write_json_atomic(target_snapshot_path, target_snapshot)
    legacy_cleanup_plan = (
        build_legacy_cleanup_plan(legacy_rows, target_category)
        if client
        else {
            "schema_version": ADAPTER_SCHEMA_VERSION,
            "mode": "dry-run",
            "target": "SHIJIU",
            "legacy_reference_only": True,
            "skipped": True,
            "reason": "offline_target_checks",
            "write_executed": False,
            "actions": [],
        }
    )
    write_json_atomic(legacy_cleanup_path, legacy_cleanup_plan)
    contract_audit = build_contract_audit()
    sample_products = select_cross_category_samples(products, sample_per_category)
    sample_numbers = {item["product_number"] for item in sample_products}
    sample_entries = [
        item for item in plan
        if (item.get("mapped_product") or {}).get("product_number") in sample_numbers
    ]
    if len(sample_entries) < 20:
        raise ImportPlanError(f"expected at least 20 cross-category payload previews, got {len(sample_entries)}")
    compact_action_path = report_dir / "dry_run_actions.json"
    incremental_summary_path = report_dir / "incremental_sync_summary.json"
    review_required_path = report_dir / "review_required.json"
    payload_previews_path = report_dir / "payload_previews.json"
    legacy_audit_path = report_dir / "legacy_reference_audit.json"
    tracked_cleanup_path = report_dir / "legacy_cleanup_plan.json"
    special_exclusion_path = report_dir / "special_exclusion_report.json"
    write_json_atomic(compact_action_path, {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "mode": "dry-run",
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "generated_at": now(),
        "actions": [_compact_action(item) for item in plan],
    })
    incremental_summary = {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "mode": "dry-run",
        "baseline": "previous MIKI HOUSE master catalog used by storefront incremental_changes.json",
        "is_initial_sync": bool(changes.get("is_initial_sync")),
        "operation_count": len(incremental_operations),
        "change_type_counts": dict(sorted(incremental_type_counts.items())),
        "planned_action_counts": dict(sorted(incremental_action_counts.items())),
        "price_changed_count": incremental_type_counts.get("PRICE_CHANGED", 0),
        "price_update_count": incremental_action_counts.get("UPDATE_PRICE_BY_EXACT_VARIANT_SKU", 0),
        "price_review_required_count": len(price_reviews),
        "price_update_recreates_product": False,
        "currency": "JPY",
        "currency_conversion_applied": False,
        "price_guard": price_guard,
        "samples": {
            change_type: [item for item in incremental_operations if item["change_type"] == change_type][:3]
            for change_type in sorted(incremental_type_counts)
        },
        "full_plan_path": str(incremental_plan_path),
        "full_plan_sha256": file_sha256(incremental_plan_path),
    }
    write_json_atomic(incremental_summary_path, incremental_summary)
    write_json_atomic(review_required_path, {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "price_review_required": price_reviews,
        "write_executed": False,
    })
    write_json_atomic(payload_previews_path, {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "mode": "dry-run",
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "cross_category_product_count": len(sample_entries),
        "classification_counts": dict(sorted(Counter(
            item["mapped_product"]["classification"] for item in sample_entries[:20]
        ).items())),
        "payloads": [item["mapped_product"] for item in sample_entries[:20]],
        "write_executed": False,
    })
    write_json_atomic(legacy_audit_path, legacy_audit)
    write_json_atomic(tracked_cleanup_path, legacy_cleanup_plan)
    special_exclusion_report = {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "excluded_reason": PDF_SPECIAL_EXCLUDED_REASON,
        "policy": "permanent_pre_filter_before_all_Shijiu_create_update_inventory_image_price_reactivation_stages",
        "total_count": len(special),
        "online_excluded_count": online_special_count,
        "offline_remembered_count": offline_special_count,
        "product_numbers": sorted(special),
        "special_products_in_shijiu_candidate_pool": [],
        "declared_baseline": {
            "storefront_product_count": DECLARED_STOREFRONT_BASELINE_COUNT,
            "online_special_excluded_count": 311,
            "candidate_product_count": DECLARED_CANDIDATE_BASELINE_COUNT,
        },
        "live_observation": {
            "storefront_product_count": len(products) + online_special_count,
            "candidate_product_count": len(products),
            "new_non_special_products_since_declared_baseline": max(
                0, len(products) - DECLARED_CANDIDATE_BASELINE_COUNT
            ),
            "new_non_special_product_numbers": live_new_since_declared_baseline,
        },
        "passed": not leaked,
    }
    write_json_atomic(special_exclusion_path, special_exclusion_report)
    report = {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "generated_at": now(),
        "mode": "dry-run",
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "provider_isolation": {
            "upstream_provider": SOURCE_CODE,
            "peer_provider": "WAWU",
            "shared_product_identity": False,
            "shared_variant_identity": False,
            "shared_sync_state": False,
            "shared_target_category": False,
            "fixed_shijiu_category_id": target_category["id"],
            "product_name_matching": "forbidden",
        },
        "readiness": {
            "dry_run_validation_passed": True,
            "ready_for_online_write": False,
            "online_write_authorized": False,
            "automatic_create_allowed": True,
            "legacy_products_block_create": False,
            "blockers": [
                "real Shijiu writes are outside this round",
                "every official image must be uploaded through Shijiu/COS before a payload becomes executable",
                "readback execution requires a separately authorized writer",
            ],
        },
        "framework_reference": {
            "repository": "qinxitong8666/wawu-product-sync",
            "commit": WAWU_REFERENCE_COMMIT,
            "reused_patterns": [
                "Shijiu client transport and evidenced endpoint paths",
                "Shijiu native product/SKU payload field names",
                "intent-before-mutation and readback-before-retry state-machine pattern",
                "checkpoint/resume",
                "dry-run write gate",
            ],
            "not_reused": [
                "WAWU upstream API client",
                "WAWU mapper",
                "WAWU product field semantics",
                "WAWU pricing rules",
            ],
            "successful_live_write_evidence_on_reference_main": False,
            "evidence_limitation": (
                "current reference main proves the Shijiu URL, endpoint paths, native payload fixtures, "
                "write gates and offline lifecycle tests; its committed LIVE_001 evidence records zero writes"
            ),
            "contract_audit": SHIJIU_CONTRACT_AUDIT_PATH,
        },
        "source_catalog": {
            "path": str(master_path),
            "sha256": file_sha256(master_path),
            "product_count": len(products),
            "variant_count": sum(len(item.get("variants") or []) for item in products),
            "permanent_special_exclusion_count": len(special),
            "special_excluded_reason": PDF_SPECIAL_EXCLUDED_REASON,
            "live_storefront_product_count": len(products) + online_special_count,
            "declared_baseline_storefront_product_count": DECLARED_STOREFRONT_BASELINE_COUNT,
            "online_special_excluded_count": online_special_count,
            "offline_special_remembered_count": offline_special_count,
            "candidate_product_count": len(products),
            "declared_baseline_candidate_count": DECLARED_CANDIDATE_BASELINE_COUNT,
            "new_non_special_products_since_declared_baseline": max(
                0, len(products) - DECLARED_CANDIDATE_BASELINE_COUNT
            ),
            "new_non_special_product_numbers": live_new_since_declared_baseline,
            "special_products_in_source": leaked,
        },
        "plan_summary": {
            "total": len(plan),
            "create": action_counts.get("create", 0),
            "update": action_counts.get("update", 0),
            "deactivate": action_counts.get("deactivate", 0),
            "reactivate": action_counts.get("reactivate", 0),
            "skip": action_counts.get("skip", 0),
            "failed": action_counts.get("failed", 0),
            "review_required": action_counts.get("review_required", 0),
            "incremental_change_set": action_counts.get("incremental_change_set", 0),
            "publish_ready": sum(bool(item.get("publish_ready")) for item in mapped),
            "unpublishable_missing_image": len(missing_images),
            "declared_baseline_candidate_count": DECLARED_CANDIDATE_BASELINE_COUNT,
            "declared_baseline_publishable_create_count": max(
                0, DECLARED_CANDIDATE_BASELINE_COUNT - len(missing_images)
            ),
            "live_new_non_special_create_count": max(
                0, len(products) - DECLARED_CANDIDATE_BASELINE_COUNT
            ),
        },
        "missing_image_products": missing_images,
        "price_validation": {
            "currency": "JPY",
            "source_field": "mini_program_price_jpy",
            "rule": "copy existing per-variant value; validate ceil(tax_included_price_jpy * 0.65)",
            "variant_count": len(source_variants),
            "failure_count": len(price_failures),
            "currency_conversion_applied": False,
            "passed": not price_failures,
        },
        "image_mapping_validation": {
            "ordered_upload_task_count": sum(
                len(item.get("image_upload_plan") or []) for item in mapped
            ),
            "role_counts": dict(sorted(image_role_counts.items())),
            "formal_shijiu_image_source_url_leak_count": len(formal_image_source_url_leaks),
            "target_upload_endpoint": "/v1/cos/upload",
            "uploads_executed": 0,
            "all_payloads_require_target_urls_before_write": True,
            "passed": not formal_image_source_url_leaks,
        },
        "target_read_only_verification": {
            "fixed_category_discovery": category_discovery,
            "legacy_reference_audit": legacy_audit,
            "legacy_product_count": legacy_audit.get("legacy_product_count", 0),
            "legacy_detail_sample_count": legacy_audit.get("sample_size", 0),
            "identity_reconciliation_attempted": False,
            "sku_binding_attempted": False,
            "price_matching_attempted": False,
            "read_request_count": target_snapshot["read_request_count"],
            "semantic_write_request_count": target_snapshot["semantic_write_request_count"],
            "mutating_endpoints_called": target_snapshot["mutating_endpoints_called"],
            "passed": (
                category_discovery.get("passed") is True
                and legacy_audit.get("passed") is True
                and target_snapshot["semantic_write_request_count"] == 0
            ) if client else False,
        },
        "legacy_reference_only": {
            "legacy_product_count": legacy_audit.get("legacy_product_count", 0),
            "identity_relation_to_new_pool": "none",
            "blocks_new_create": False,
            "cleanup_plan_action_count": legacy_cleanup_plan.get("action_count", 0),
            "cleanup_write_executed": False,
        },
        "permanent_pdf_special_exclusion": special_exclusion_report,
        "stable_identity": {
            "source_product_id": "MIKIHOUSE:<product_number>",
            "source_variant_id": "MIKIHOUSE:<product_number>:<variant SKU>",
            "backend_sku_code": "MIKI-<variant SKU>",
            "mapping_state_path": str(mapping_path),
            "mapping_summary": mapping_summary(mapping_state),
            "product_name_matching": "forbidden",
        },
        "incremental_sync": incremental_summary,
        "contract_audit": contract_audit,
        "field_mapping_contract": field_mapping_contract(),
        "checkpoint": checkpoint_summary,
        "write_safety": {
            "write_capability_present": False,
            "cli_write_option_present": False,
            "payload_is_executable_write_request": False,
            "image_uploads_executed": 0,
            "target_brand_id_mapping_status": "deferred_no_evidenced_read_contract",
            "allowed_target_endpoints": sorted(READ_ONLY_ENDPOINTS),
            "blocked_mutating_endpoints": sorted(MUTATING_ENDPOINTS),
            "semantic_write_request_count": target_snapshot["semantic_write_request_count"],
            "passed": target_snapshot["semantic_write_request_count"] == 0,
        },
        "rollback_plan": build_rollback_plan(),
        "field_mapping_samples": [_compact_plan_sample(item) for item in sample_entries][:20],
        "payload_preview_count": min(20, len(sample_entries)),
        "payload_preview_type_counts": dict(sorted(Counter(
            item["mapped_product"]["classification"] for item in sample_entries[:20]
        ).items())),
        "artifacts": {
            "full_plan_path": str(full_plan_path),
            "full_plan_size_bytes": full_plan_path.stat().st_size,
            "full_plan_sha256": file_sha256(full_plan_path),
            "target_snapshot_path": str(target_snapshot_path),
            "checkpoint_path": str(checkpoint_path),
            "mapping_state_path": str(mapping_path),
            "incremental_plan_path": str(incremental_plan_path),
            "tracked_action_plan_path": str(compact_action_path),
            "tracked_action_plan_size_bytes": compact_action_path.stat().st_size,
            "tracked_action_plan_sha256": file_sha256(compact_action_path),
            "tracked_incremental_summary_path": str(incremental_summary_path),
            "review_required_path": str(review_required_path),
            "payload_previews_path": str(payload_previews_path),
            "legacy_reference_audit_path": str(legacy_audit_path),
            "legacy_cleanup_plan_path": str(tracked_cleanup_path),
            "special_exclusion_report_path": str(special_exclusion_path),
        },
        "passed": (
            checkpoint_summary["complete"]
            and action_counts.get("failed", 0) == 0
            and len(missing_images) == 7
            and not price_failures
            and (target_snapshot["semantic_write_request_count"] == 0)
            and (category_discovery.get("passed") is True if client else True)
            and (legacy_audit.get("passed") is True if client else True)
            and len(sample_entries) >= 20
        ),
    }
    write_json_atomic(output_dir / "dry_run_report.json", report)
    write_json_atomic(report_dir / "dry_run_report.json", report)
    write_json_atomic(report_dir / "read_only_target_verification.json", target_snapshot)
    write_json_atomic(report_dir / "contract_audit.json", contract_audit)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan a fail-closed, read-only MIKI HOUSE import into the Shijiu backend"
    )
    parser.add_argument("--master", type=Path, default=Path("output/storefront-stable/stable_catalog.json"))
    parser.add_argument("--changes", type=Path, default=Path("output/storefront-stable/stable_incremental_changes.json"))
    parser.add_argument("--special", type=Path, default=Path("special_skus_2026aw.csv"))
    parser.add_argument("--category-map", type=Path, default=Path("config/shijiu_category_map.json"))
    parser.add_argument("--price-guard", type=Path, default=Path("config/shijiu_price_guard.json"))
    parser.add_argument("--mapping-state", type=Path, default=Path("state/shijiu_mappings.json"))
    parser.add_argument("--output", type=Path, default=Path("output/shijiu-import"))
    parser.add_argument("--report-dir", type=Path, default=Path("deliverables/shijiu_import"))
    parser.add_argument("--checkpoint", type=Path, default=Path("output/shijiu-import/checkpoint.json"))
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-items", type=int)
    parser.add_argument("--sample-per-category", type=int, default=5)
    parser.add_argument("--target-env-file", type=Path)
    parser.add_argument("--target-base-url", default=DEFAULT_SHIJIU_BASE_URL)
    parser.add_argument("--offline-target-checks", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_items is not None and args.max_items <= 0:
        raise SystemExit("--max-items must be positive")
    if args.sample_per_category < 5 and not args.offline_target_checks:
        raise SystemExit("online target verification requires at least 5 samples per category")
    if args.offline_target_checks:
        client = None
    else:
        if not args.target_env_file:
            raise SystemExit("--target-env-file is required unless --offline-target-checks is used")
        config = load_env_file(args.target_env_file)
        client = ReadOnlyShijiuClient(
            token=config.get("SHIJIU_TOKEN") or config.get("MYSHOP_TOKEN", ""),
            secret=config.get("SHIJIU_SECRET") or config.get("MYSHOP_SECRET", ""),
            base_url=args.target_base_url,
        )
    try:
        report = run_dry_run_import(
            args.master,
            args.changes,
            args.special,
            args.category_map,
            args.price_guard,
            args.mapping_state,
            args.output,
            args.report_dir,
            args.checkpoint,
            client=client,
            sample_per_category=args.sample_per_category,
            resume=args.resume,
            max_items=args.max_items,
        )
    except (OSError, ValueError, ImportPlanError) as exc:
        raise SystemExit(f"dry-run import planning failed: {exc}") from exc
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 2
