from __future__ import annotations

import argparse
import copy
import hashlib
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


ADAPTER_SCHEMA_VERSION = 1
SOURCE_CODE = "MIKIHOUSE"
WAWU_REFERENCE_COMMIT = "a36c5eab40bf419562ba03d15c090151698d582a"
SHIJIU_CONTRACT_AUDIT_PATH = "docs/shijiu_downstream_contract_audit.md"
DEFAULT_SHIJIU_BASE_URL = "https://api.wfcorp.cn/shijiu"
READ_ONLY_ENDPOINTS = frozenset({
    "/shopapi/Goods/index",
    "/shopapi/goods/getFormatInfo",
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
                "User-Agent": "Mozilla/5.0 (compatible; mikihouse-luyao/0.5; read-only)",
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

    def search_products(self, *, sku_code: str = "", good_name: str = "", page_size: int = 20) -> dict[str, Any]:
        return self.request_read(
            "/shopapi/Goods/index",
            {
                "page": 1,
                "page_size": page_size,
                "good_type": "",
                "father_type": "",
                "recommend": "",
                "good_name": good_name,
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


def recursively_find_sku_codes(value: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"sku_code", "good_code"} and child not in (None, ""):
                result.add(str(child).strip())
            result.update(recursively_find_sku_codes(child))
    elif isinstance(value, list):
        for item in value:
            result.update(recursively_find_sku_codes(item))
    return result


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


def load_category_map(path: Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("source") != SOURCE_CODE or payload.get("target") != "SHIJIU":
        raise ImportPlanError("category map must explicitly declare MIKIHOUSE -> SHIJIU")
    categories = payload.get("target_categories") or {}
    required = {"footwear", "apparel", "baby", "goods", "other"}
    if set(categories) != required:
        raise ImportPlanError(f"category map keys must be {sorted(required)}")
    for key, value in categories.items():
        if not isinstance(value.get("id"), int) or not str(value.get("name") or "").strip():
            raise ImportPlanError(f"invalid target category mapping: {key}")
    return categories


def validate_live_categories(
    category_map: dict[str, dict[str, Any]], response: dict[str, Any]
) -> list[dict[str, Any]]:
    live = {int(item["id"]): str(item.get("type_name") or "") for item in response_rows(response)}
    checks = []
    for classification, mapping in category_map.items():
        actual = live.get(mapping["id"])
        checks.append({
            "classification": classification,
            "target_category_id": mapping["id"],
            "expected_name": mapping["name"],
            "actual_name": actual,
            "passed": actual == mapping["name"],
        })
    if not all(item["passed"] for item in checks):
        raise ImportPlanError(f"target category mapping no longer matches live categories: {checks}")
    return checks


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


def _product_image_urls(product: dict[str, Any]) -> list[str]:
    result: list[str] = []

    def add(image: dict[str, Any] | None) -> None:
        url = str((image or {}).get("url") or "").strip()
        if url and url not in result:
            result.append(url)

    add(product.get("main_image"))
    for color in product.get("color_images") or []:
        for item in color.get("images") or []:
            add(item.get("image"))
    for variant in product.get("variants") or []:
        add(variant.get("resolved_image"))
    return result


def map_product_to_shijiu(
    product: dict[str, Any],
    category_map: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    product_number = str(product["product_number"])
    classification = _classification_name(product)
    target_category = category_map[classification]
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
            "sku_thumbnail": image_url,
            "weight": "",
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
    images = _product_image_urls(product)
    cover = str((product.get("main_image") or {}).get("url") or "")
    if not cover and images:
        cover = images[0]
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
        "good_details": "",
        "state": "1" if product.get("active") else "0",
        "spec_name": spec_name,
        "sku_info": sku_info,
        "vnarious": 1,
        "good_type": target_category["id"],
        "good_detail_pics": ",".join(images),
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
        "master_graph": cover,
        "broadcast": ",".join(images),
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
        "classification": classification,
        "target_category": target_category,
        "currency": "JPY",
        "currency_conversion_applied": False,
        "publish_ready": publish_ready,
        "publish_blockers": [] if publish_ready else ["missing_official_image"],
        "source_variants": source_variants,
        "image_upload_plan": [
            {"source_url": url, "operation": "upload_before_future_write"} for url in images
        ],
        "shijiu_payload_preview": payload,
        "payload_sha256": content_sha256(payload),
        "minimum_mini_program_price_jpy": min_price,
        "future_write_dependencies": [
            "upload every official source image through the evidenced Shijiu COS endpoint",
            "replace image preview URLs with returned Shijiu URLs",
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


def target_check_product(
    client: ReadOnlyShijiuClient,
    product: dict[str, Any],
) -> dict[str, Any]:
    first_variant = next(
        (item for item in product.get("variants") or [] if item.get("sku")), None
    )
    if first_variant is None:
        raise ImportPlanError(f"sample product has no SKU: {product['product_number']}")
    desired_code = backend_sku_code(str(first_variant["sku"]))
    response = client.search_products(sku_code=desired_code, page_size=20)
    rows = response_rows(response)
    matched = None
    detail = None
    detail_checks = None
    for row in rows[:20]:
        row_code = str(row.get("good_code") or row.get("sku_code") or "").strip()
        if row_code == desired_code:
            matched = row
            break
        backend_id = row.get("id") or row.get("good_id") or row.get("goods_id")
        if backend_id:
            candidate_detail = client.product_detail(backend_id)
            if desired_code in recursively_find_sku_codes(candidate_detail):
                matched = row
                detail = candidate_detail
                break
    if matched and detail is None:
        backend_id = matched.get("id") or matched.get("good_id") or matched.get("goods_id")
        if backend_id:
            detail = client.product_detail(backend_id)
    if detail is not None:
        detail_checks = {
            "desired_sku_found": desired_code in recursively_find_sku_codes(detail),
            "returned_sku_count": len(recursively_find_skus(detail)),
        }
    return {
        "product_number": product["product_number"],
        "classification": _classification_name(product),
        "query_sku_code": desired_code,
        "target_response_count": int(response.get("count") or len(rows)),
        "target_rows_examined": len(rows[:20]),
        "matched": bool(matched),
        "backend_product_id": (
            str(matched.get("id") or matched.get("good_id") or matched.get("goods_id"))
            if matched
            else None
        ),
        "detail_checks": detail_checks,
    }


def discover_shijiu_read_contract(client: ReadOnlyShijiuClient) -> dict[str, Any]:
    """Read one existing Shijiu row/detail and retain schema evidence, not business values."""
    listing = client.search_products(page_size=1)
    rows = response_rows(listing)
    if not rows:
        return {
            "passed": False,
            "reason": "Shijiu product list returned no rows",
            "active_catalog_count": int(listing.get("count") or 0),
        }
    row = rows[0]
    backend_id = row.get("id") or row.get("good_id") or row.get("goods_id")
    if backend_id in (None, ""):
        return {
            "passed": False,
            "reason": "Shijiu product list row has no backend product id",
            "active_catalog_count": int(listing.get("count") or len(rows)),
            "list_row_keys": sorted(row),
        }
    detail = client.product_detail(backend_id)
    sku_rows = recursively_find_skus(detail)
    return {
        "passed": isinstance(detail, dict) and bool(detail),
        "active_catalog_count": int(listing.get("count") or len(rows)),
        "backend_product_id_observed": True,
        "list_row_keys": sorted(row),
        "detail_root_keys": sorted(detail),
        "detail_sku_count": len(sku_rows),
        "detail_sku_field_keys": sorted({key for sku in sku_rows for key in sku}),
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
        return {"schema_version": 1, "source": SOURCE_CODE, "mappings": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("source") != SOURCE_CODE or not isinstance(payload.get("mappings"), dict):
        raise ImportPlanError("invalid Shijiu mapping state")
    return payload


def choose_action(
    mapped: dict[str, Any],
    mapping: dict[str, Any] | None,
    target_check: dict[str, Any] | None,
) -> tuple[str, str]:
    if not mapped["publish_ready"]:
        return "skip", "missing_official_image"
    if not mapped["shijiu_payload_preview"]["state"] == "1":
        if mapping or (target_check and target_check.get("matched")):
            return "deactivate", "source_product_inactive"
        return "skip", "inactive_source_not_present_on_target"
    if mapping:
        if mapping.get("target_active") is False:
            return "reactivate", "stable_mapping_marks_target_inactive"
        if mapping.get("last_payload_sha256") == mapped["payload_sha256"]:
            return "skip", "payload_unchanged_since_verified_sync"
        return "update", "stable_source_mapping_exists"
    if target_check and target_check.get("matched"):
        return "update", "read_only_target_match_by_stable_sku"
    return "create", "no_stable_mapping_or_target_sample_match"


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
            "read-only discovery and non-executable mapping previews only; image uploads and every Shijiu mutation are absent"
        ),
    }


def field_mapping_contract() -> dict[str, Any]:
    return {
        "source": "MIKI HOUSE master_catalog.json",
        "target": "Shijiu native field preview",
        "product_name": "name -> good_name",
        "brand": "brand -> adapter source_brand + good_describe; brand_id deferred",
        "category": "classification -> verified Shijiu father category good_type",
        "main_image": "main_image.url -> master_graph preview; future COS upload required",
        "color_images": "color_images/resolved_image -> broadcast and sku_thumbnail previews; future COS upload required",
        "options": "selected_options/color/size -> spec_name dimensions and per-SKU spec_name",
        "sku": "variant sku -> MIKI-<sku> in sku_code",
        "inventory": "availableForSale boolean -> sku_stock 1/0; source boolean retained",
        "price": "mini_program_price_jpy -> sku_price and member price fields, currency JPY",
        "source_identity": "product_number and product_number+variant SKU -> stable source IDs",
    }


def plan_import(
    products: list[dict[str, Any]],
    changes: dict[str, Any],
    category_map: dict[str, dict[str, Any]],
    mapping_state: dict[str, Any],
    target_checks: dict[str, dict[str, Any]],
    checkpoint_path: Path,
    *,
    resume: bool = False,
    max_items: int | None = None,
    checkpoint_interval: int = 25,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
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
    selected = [item for item in products if item["product_number"] in affected]
    records: dict[str, Any] = checkpoint["records"]
    processed_this_run = 0
    for product in selected:
        stable_product_id = source_product_id(product["product_number"])
        if stable_product_id in records and records[stable_product_id].get("status") == "planned":
            continue
        try:
            mapped = map_product_to_shijiu(product, category_map)
            mapping = mapping_state["mappings"].get(stable_product_id)
            target_check = target_checks.get(product["product_number"])
            action, reason = choose_action(mapped, mapping, target_check)
            record = {
                "status": "planned",
                "action": action,
                "reason": reason,
                "target_check_status": "checked" if target_check else "not_sampled_this_run",
                "payload_sha256": mapped["payload_sha256"],
                "existing_backend_product_id": (
                    mapping.get("backend_product_id") if mapping else None
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
                entry["target_check"] = target_checks.get(product["product_number"])
                entry["existing_mapping"] = mapping_state["mappings"].get(stable_product_id)
                entry["mapped_product"] = map_product_to_shijiu(product, category_map)
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
        "source_product_id", "status", "action", "reason", "target_check_status", "target_check"
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
        "target_checked": bool(entry.get("target_check")),
    }


def run_dry_run_import(
    master_path: Path,
    changes_path: Path,
    special_path: Path,
    category_map_path: Path,
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
    products = list(master.get("products") or [])
    special = set(read_product_numbers(special_path))
    if len(special) != 351:
        raise ImportPlanError(f"expected 351 permanent exclusions, got {len(special)}")
    leaked = sorted({item["product_number"] for item in products} & special)
    if leaked:
        raise ImportPlanError(f"special products leaked into import source: {leaked}")
    category_map = load_category_map(category_map_path)
    category_checks: list[dict[str, Any]] = []
    target_checks: list[dict[str, Any]] = []
    target_contract_discovery: dict[str, Any] = {}
    if client:
        category_checks = validate_live_categories(category_map, client.categories())
        target_contract_discovery = discover_shijiu_read_contract(client)
        for product in select_cross_category_samples(products, sample_per_category):
            target_checks.append(target_check_product(client, product))
    target_check_map = {item["product_number"]: item for item in target_checks}
    mapping_state = load_mapping_state(mapping_path)
    plan, checkpoint_summary = plan_import(
        products,
        changes,
        category_map,
        mapping_state,
        target_check_map,
        checkpoint_path,
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
    output_dir.mkdir(parents=True, exist_ok=True)
    full_plan_path = output_dir / "dry_run_import_plan.json"
    target_snapshot_path = output_dir / "read_only_target_snapshot.json"
    write_json_atomic(full_plan_path, {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "mode": "dry-run",
        "generated_at": now(),
        "plan": plan,
    })
    target_snapshot = {
        "target": "SHIJIU",
        "checked_at": now(),
        "category_checks": category_checks,
        "read_contract_discovery": target_contract_discovery,
        "product_checks": target_checks,
        "request_ledger": client.requests if client else [],
        "read_request_count": len(client.requests) if client else 0,
        "semantic_write_request_count": client.semantic_write_request_count if client else 0,
        "mutating_endpoints_called": [
            item["path"] for item in (client.requests if client else []) if item["path"] in MUTATING_ENDPOINTS
        ],
    }
    write_json_atomic(target_snapshot_path, target_snapshot)
    contract_audit = build_contract_audit()
    checked_plan_entries = [item for item in plan if item.get("target_check")]
    sample_entries = checked_plan_entries or [item for item in plan if item.get("mapped_product")][:20]
    compact_action_path = report_dir / "dry_run_actions.json"
    write_json_atomic(compact_action_path, {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "mode": "dry-run",
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "generated_at": now(),
        "actions": [_compact_action(item) for item in plan],
    })
    report = {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "generated_at": now(),
        "mode": "dry-run",
        "source": SOURCE_CODE,
        "target": "SHIJIU",
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
            "publish_ready": sum(bool(item.get("publish_ready")) for item in mapped),
            "unpublishable_missing_image": len(missing_images),
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
        "target_read_only_verification": {
            "sample_product_count": len(target_checks),
            "type_counts": dict(sorted(Counter(item["classification"] for item in target_checks).items())),
            "matched_existing_count": sum(bool(item["matched"]) for item in target_checks),
            "category_checks": category_checks,
            "read_contract_discovery": target_contract_discovery,
            "read_request_count": target_snapshot["read_request_count"],
            "semantic_write_request_count": target_snapshot["semantic_write_request_count"],
            "mutating_endpoints_called": target_snapshot["mutating_endpoints_called"],
            "passed": (
                len(target_checks) >= 20
                and all(item["passed"] for item in category_checks)
                and target_contract_discovery.get("passed") is True
                and target_snapshot["semantic_write_request_count"] == 0
            ) if client else False,
        },
        "stable_identity": {
            "source_product_id": "MIKIHOUSE:<product_number>",
            "source_variant_id": "MIKIHOUSE:<product_number>:<variant SKU>",
            "backend_sku_code": "MIKI-<variant SKU>",
            "mapping_state_path": str(mapping_path),
        },
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
        "artifacts": {
            "full_plan_path": str(full_plan_path),
            "full_plan_size_bytes": full_plan_path.stat().st_size,
            "full_plan_sha256": file_sha256(full_plan_path),
            "target_snapshot_path": str(target_snapshot_path),
            "checkpoint_path": str(checkpoint_path),
            "tracked_action_plan_path": str(compact_action_path),
            "tracked_action_plan_size_bytes": compact_action_path.stat().st_size,
            "tracked_action_plan_sha256": file_sha256(compact_action_path),
        },
        "passed": (
            checkpoint_summary["complete"]
            and action_counts.get("failed", 0) == 0
            and len(missing_images) == 7
            and not price_failures
            and (target_snapshot["semantic_write_request_count"] == 0)
            and (target_contract_discovery.get("passed") is True if client else True)
            and (len(target_checks) >= 20 if client else True)
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
    parser.add_argument("--master", type=Path, default=Path("output/storefront-master/master_catalog.json"))
    parser.add_argument("--changes", type=Path, default=Path("output/storefront-master/incremental_changes.json"))
    parser.add_argument("--special", type=Path, default=Path("special_skus_2026aw.csv"))
    parser.add_argument("--category-map", type=Path, default=Path("config/shijiu_category_map.json"))
    parser.add_argument("--mapping-state", type=Path, default=Path("output/shijiu-import/mappings.json"))
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
