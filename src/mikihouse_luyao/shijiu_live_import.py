from __future__ import annotations

import copy
import gzip
import hashlib
import json
import mimetypes
import os
import re
import string
import time
import urllib.parse
import urllib.request
import uuid
import zlib
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from .csv_input import read_product_numbers
from .shijiu_import import (
    DEFAULT_SHIJIU_BASE_URL,
    EXPECTED_SPECIAL_COUNT,
    PDF_SPECIAL_EXCLUDED_REASON,
    SOURCE_CODE,
    ImportPlanError,
    canonical_json,
    content_sha256,
    load_env_file,
    load_mapping_state,
    now,
    recursively_find_skus,
    response_rows,
    validate_live_mikihouse_category,
    write_json_atomic,
)


LIVE_BATCH_SCHEMA_VERSION = 1
LIVE_WRITE_CONFIRMATION = "MIKIHOUSE_FIRST_20_REAL_IMPORT"
CREATE_PATH = "/shopapi/Goods/newAddGood"
IMAGE_UPLOAD_PATH = "/v1/cos/upload"
DETAIL_PATH = "/shopapi/goods/getFormatInfo"
LIST_PATH = "/shopapi/Goods/index"
CATEGORY_PATH = "/shopapi/Goodtype/typeindex"
PLACEHOLDER_PATTERN = re.compile(r"\{\{SHIJIU_COS_URL:([^}]+)}}")
TARGET_SKU_ID_FIELDS = ("sku_id", "goods_sku_id", "good_sku_id", "id")
NATIVE_SAVE_FALLBACK_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json;charset=UTF-8",
    "origin": "https://shijiu.wfcorp.cn",
    "referer": "https://shijiu.wfcorp.cn/",
    "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
    ),
}
OFFICIAL_MIKIHOUSE_IMAGE_HOST_SUFFIXES = (
    "shopify.com",
    "mikihouse.co.jp",
    # Detail media returned by the official MIKI HOUSE Storefront product data.
    "img.mksk.me",
)
CANONICAL_CREATE_CONTRACT_PATH = (
    Path(__file__).resolve().parents[2] / "config/shijiu_native_create_contract.json"
)


class LiveImportError(ImportPlanError):
    """Fail-closed error: the batch must not continue after this exception."""


class ContractMismatchError(LiveImportError):
    pass


class DuplicateRiskError(LiveImportError):
    pass


def _json_value_type(value: Any) -> str:
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, bool):
        return "boolean"
    if value is None:
        return "null"
    if isinstance(value, (int, float, Decimal)):
        return "number"
    return "string" if isinstance(value, str) else type(value).__name__


def validate_canonical_create_payload(payload: dict[str, Any]) -> None:
    """Fail closed unless a payload exactly matches the persisted browser CREATE shape."""
    contract = json.loads(CANONICAL_CREATE_CONTRACT_PATH.read_text(encoding="utf-8"))
    if list(payload) != contract["product_fields"]:
        raise ContractMismatchError("create payload top-level field order differs from canonical browser CREATE")
    actual_types = {key: _json_value_type(value) for key, value in payload.items()}
    if actual_types != contract["product_field_types"]:
        raise ContractMismatchError("create payload top-level field types differ from canonical browser CREATE")
    if payload.get("state") != contract["state"] or payload.get("is_shelf") != contract["is_shelf"]:
        raise ContractMismatchError("create payload state/is_shelf differs from canonical browser CREATE")
    if not payload.get("sku_info") or not payload.get("spec_name"):
        raise ContractMismatchError("canonical create requires non-empty sku_info and spec_name")
    for row in payload["sku_info"]:
        if list(row) != contract["sku_fields"]:
            raise ContractMismatchError("create payload SKU field order differs from canonical browser CREATE")
        if {key: _json_value_type(value) for key, value in row.items()} != contract["sku_field_types"]:
            raise ContractMismatchError("create payload SKU field types differ from canonical browser CREATE")
    for row in payload["spec_name"]:
        if list(row) != contract["spec_fields"]:
            raise ContractMismatchError("create payload specification field order differs from canonical browser CREATE")
        if {key: _json_value_type(value) for key, value in row.items()} != contract["spec_field_types"]:
            raise ContractMismatchError("create payload specification field types differ from canonical browser CREATE")


def validate_canonical_update_payload(payload: dict[str, Any]) -> None:
    """Validate the audited native full-payload edit shape.

    Shijiu uses the same save endpoint for CREATE and edit.  The only permitted
    edit-only business field is the existing integer product ``id``; every
    canonical CREATE field (including complete specs/SKUs) must remain present
    in its browser-observed order and type.
    """
    product_id = payload.get("id")
    if not isinstance(product_id, int):
        raise ContractMismatchError("native edit payload requires an integer product id")
    if list(payload)[-1:] != ["id"]:
        raise ContractMismatchError("native edit product id must follow the canonical full payload")
    create_shape = {key: value for key, value in payload.items() if key != "id"}
    validate_canonical_create_payload(create_shape)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redacted_response(value: Any) -> Any:
    """Keep response structure needed for evidence without persisting huge bodies."""
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if str(key).casefold() in {"token", "secret", "cookie", "authorization"}:
                result[key] = "<redacted>"
            elif str(key) in {"good_details"} and isinstance(child, str) and len(child) > 1000:
                result[key] = child[:1000] + "<truncated>"
            else:
                result[key] = _redacted_response(child)
        return result
    if isinstance(value, list):
        return [_redacted_response(item) for item in value]
    return value


def _decode_response(response: Any, raw: bytes) -> str:
    encoding = (response.headers.get("Content-Encoding") or "").lower()
    if encoding == "gzip":
        raw = gzip.decompress(raw)
    elif encoding == "deflate":
        raw = zlib.decompress(raw)
    elif encoding in {"br", "zstd"}:
        raise ContractMismatchError(f"unsupported Shijiu response compression: {encoding}")
    return raw.decode("utf-8", errors="replace")


def _parse_json_response(response: Any, raw: bytes, operation: str) -> dict[str, Any]:
    text = _decode_response(response, raw)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractMismatchError(f"{operation} returned non-JSON data") from exc
    if not isinstance(payload, dict):
        raise ContractMismatchError(f"{operation} returned a non-object JSON value")
    return payload


def _assert_success(response: dict[str, Any], operation: str) -> None:
    # Live read-only discovery on 2026-09-04 established code=1/msg=查询成功.
    if str(response.get("code")) != "1":
        raise ContractMismatchError(
            f"{operation} did not return the evidenced Shijiu success code 1: "
            f"code={response.get('code')!r}, msg={response.get('msg')!r}"
        )


def _find_url(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("url", "path", "src", "data", "file", "image", "img"):
            if key in value:
                found = _find_url(value[key])
                if found:
                    return found
        for child in value.values():
            found = _find_url(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_url(child)
            if found:
                return found
    elif isinstance(value, str) and value.startswith(("https://", "http://")):
        return value
    return ""


def _product_id_from_value(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for container in (value, value.get("data")):
        if isinstance(container, int) or (isinstance(container, str) and container.isdigit()):
            return str(container)
        if isinstance(container, dict):
            for key in ("goods_id", "good_id", "product_id", "id"):
                candidate = container.get(key)
                if isinstance(candidate, int) or (
                    isinstance(candidate, str) and candidate.isdigit()
                ):
                    return str(candidate)
    return None


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ContractMismatchError(f"invalid numeric value in Shijiu readback: {value!r}") from exc


def _first_observation(value: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        for key in keys:
            if value.get(key) not in (None, ""):
                return value[key]
        for child in value.values():
            found = _first_observation(child, keys)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_observation(child, keys)
            if found not in (None, ""):
                return found
    return None


def _split_urls(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _normalized_specification(value: Any) -> str:
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                part = item.get("spec_name") or item.get("name") or item.get("value") or ""
            else:
                part = item
            text = str(part).strip()
            if text:
                parts.append(text)
        return ",".join(parts)
    return str(value or "").strip()


class ShijiuLiveClient:
    """Minimal MIKIHOUSE-only Shijiu transport with an explicit write gate."""

    def __init__(
        self,
        token: str,
        secret: str,
        *,
        base_url: str = DEFAULT_SHIJIU_BASE_URL,
        cookie: str = "",
        timeout: float = 90,
        write_confirmation: str = LIVE_WRITE_CONFIRMATION,
        request_observer: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if not token or not secret:
            raise LiveImportError("missing SHIJIU_TOKEN/SHIJIU_SECRET credentials")
        self.token = token
        self.secret = secret
        self.base_url = base_url.rstrip("/")
        parsed = urllib.parse.urlparse(self.base_url)
        self.api_origin = f"{parsed.scheme}://{parsed.netloc}"
        self.cookie = cookie
        self.timeout = timeout
        self.write_confirmation = write_confirmation
        self.request_observer = request_observer
        self.requests: list[dict[str, Any]] = []

    def _endpoint(self, path: str) -> str:
        return f"{self.base_url}{path}&token={urllib.parse.quote(self.token)}"

    def _record(self, path: str, semantic_operation: str, metadata: dict[str, Any]) -> None:
        item = {
            "sequence": len(self.requests) + 1,
            "at": _utc_now(),
            "method": "POST",
            "path": path,
            "semantic_operation": semantic_operation,
            **metadata,
        }
        self.requests.append(item)
        if self.request_observer:
            self.request_observer(copy.deepcopy(item))

    def _post_form(self, path: str, payload: dict[str, Any], *, operation: str) -> dict[str, Any]:
        self._record(path, "read", {"operation": operation})
        body = urllib.parse.urlencode({"secret": self.secret, "token": self.token, **payload}).encode()
        request = urllib.request.Request(
            self._endpoint(path),
            data=body,
            method="POST",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Origin": "https://shijiu.wfcorp.cn",
                "Referer": "https://shijiu.wfcorp.cn/",
                "User-Agent": "Mozilla/5.0 (compatible; mikihouse-luyao/0.8; Shijiu validation)",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            result = _parse_json_response(response, response.read(), operation)
        _assert_success(result, operation)
        return result

    def categories(self) -> dict[str, Any]:
        return self._post_form(CATEGORY_PATH, {"page": 1}, operation="category discovery")

    def search_products(
        self,
        sku_code: str = "",
        *,
        good_name: str = "",
        status: str = "",
        push: str = "2",
        good_type: int | str = 294884,
        page: int = 1,
        page_size: int = 20,
        **filters: Any,
    ) -> dict[str, Any]:
        return self._post_form(
            LIST_PATH,
            {
                "page": page,
                "page_size": page_size,
                "good_type": good_type,
                "father_type": "",
                "recommend": "",
                "good_name": good_name,
                "good_code": sku_code,
                "push": push,
                "status": status,
                "update_start_time": "",
                "update_end_time": "",
                "create_start_time": "",
                "create_end_time": "",
                "group_id": "",
                **{
                    key: value
                    for key, value in filters.items()
                    if key in {"is_delete", "audit_status", "state"}
                },
            },
            operation="exact MIKIHOUSE SKU search",
        )

    def product_detail(self, product_id: str | int) -> dict[str, Any]:
        return self._post_form(DETAIL_PATH, {"id": product_id}, operation="product readback")

    def upload_image(self, source_url: str, *, confirmation: str) -> tuple[str, dict[str, Any]]:
        self._require_write_confirmation(confirmation)
        image_data, filename, mime_type = self._download_official_image(source_url)
        boundary_seed = uuid.uuid4().hex + uuid.uuid4().hex
        alphabet = string.ascii_letters + string.digits
        tail = "".join(alphabet[int(boundary_seed[i:i + 2], 16) % len(alphabet)] for i in range(16))
        boundary = f"----WebKitFormBoundary{tail}"
        crlf = "\r\n"
        parts = [
            f"--{boundary}",
            'Content-Disposition: form-data; name="dir_name"',
            "",
            "shop",
            f"--{boundary}",
            f'Content-Disposition: form-data; name="photo"; filename="{filename}"',
            f"Content-Type: {mime_type}",
            "",
        ]
        body = (
            crlf.join(parts).encode()
            + crlf.encode()
            + image_data
            + crlf.encode()
            + f"--{boundary}--{crlf}".encode()
        )
        self._record(
            IMAGE_UPLOAD_PATH,
            "write",
            {
                "operation": "official image upload",
                "source_url_sha256": hashlib.sha256(source_url.encode()).hexdigest(),
                "byte_count": len(image_data),
            },
        )
        request = urllib.request.Request(
            f"{self.api_origin}{IMAGE_UPLOAD_PATH}",
            data=body,
            method="POST",
            headers={
                "Accept": "*/*",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Origin": "https://shijiu.wfcorp.cn",
                "Referer": "https://shijiu.wfcorp.cn/",
                "User-Agent": "Mozilla/5.0 (compatible; mikihouse-luyao/0.8; Shijiu import)",
            },
        )
        with urllib.request.urlopen(request, timeout=max(self.timeout, 120)) as response:
            result = _parse_json_response(response, response.read(), "COS image upload")
        target_url = _find_url(result)
        if not target_url.startswith("https://"):
            raise ContractMismatchError("COS upload did not return an absolute HTTPS URL")
        if urllib.parse.urlparse(target_url).netloc == urllib.parse.urlparse(source_url).netloc:
            raise ContractMismatchError("COS upload returned the original MIKI HOUSE image host")
        return target_url, result

    def create_product(self, payload: dict[str, Any], *, confirmation: str) -> dict[str, Any]:
        self._require_write_confirmation(confirmation)
        validate_canonical_create_payload(payload)
        self._record(
            CREATE_PATH,
            "write",
            {
                "operation": "create off-shelf MIKIHOUSE product",
                "payload_sha256": content_sha256(payload),
            },
        )
        body = json.dumps(
            {"secret": self.secret, "token": self.token, **payload},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        request = urllib.request.Request(
            self._endpoint(CREATE_PATH),
            data=body,
            method="POST",
            headers=self.native_save_headers(),
        )
        with urllib.request.urlopen(request, timeout=max(self.timeout, 120)) as response:
            result = _parse_json_response(response, response.read(), "product create")
        return result

    def native_save_headers(self, *, redact: bool = False) -> dict[str, str]:
        """Return the audited Shijiu native-save fallback header contract."""
        headers = dict(NATIVE_SAVE_FALLBACK_HEADERS)
        if self.cookie:
            headers["cookie"] = self.cookie
        if redact and "cookie" in headers:
            headers["cookie"] = "<redacted>"
        return headers

    def native_save_request_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        body_payload = {"secret": self.secret, "token": self.token, **payload}
        body = json.dumps(
            body_payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return {
            "endpoint": CREATE_PATH,
            "method": "POST",
            "headers": self.native_save_headers(redact=True),
            "content_type": self.native_save_headers()["content-type"],
            "serialization": {
                "format": "JSON",
                "encoding": "UTF-8",
                "ensure_ascii": False,
                "separators": [",", ":"],
                "body_auth_key_order": ["secret", "token"],
                "body_key_order_after_auth": list(payload),
                "token_also_in_query": True,
            },
            "payload_sha256": content_sha256(payload),
            "body_byte_count": len(body),
        }

    def _native_save_product(
        self,
        payload: dict[str, Any],
        *,
        confirmation: str,
        operation: str,
    ) -> dict[str, Any]:
        self._require_write_confirmation(confirmation)
        self._record(
            CREATE_PATH,
            "write",
            {
                "operation": operation,
                "payload_sha256": content_sha256(payload),
            },
        )
        preview = self.native_save_request_preview(payload)
        body = json.dumps(
            {"secret": self.secret, "token": self.token, **payload},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            self._endpoint(CREATE_PATH),
            data=body,
            method="POST",
            headers=self.native_save_headers(),
        )
        with urllib.request.urlopen(request, timeout=max(self.timeout, 120)) as response:
            raw = response.read()
            result = _parse_json_response(response, raw, operation)
            response_contract = {
                "http_status": getattr(response, "status", None),
                "content_type": response.headers.get("Content-Type"),
                "content_encoding": response.headers.get("Content-Encoding"),
                "response_byte_count": len(raw),
            }
        result["_native_request"] = preview
        result["_native_response"] = response_contract
        return result

    def create_product_native(
        self, payload: dict[str, Any], *, confirmation: str
    ) -> dict[str, Any]:
        if "id" in payload:
            raise LiveImportError("native create payload must not contain an existing product id")
        validate_canonical_create_payload(payload)
        return self._native_save_product(
            payload,
            confirmation=confirmation,
            operation="native minimal MIKIHOUSE product create",
        )

    def update_product_native(
        self, payload: dict[str, Any], *, confirmation: str
    ) -> dict[str, Any]:
        product_id = payload.get("id")
        if isinstance(product_id, str) and product_id.isdigit():
            product_id = int(product_id)
        if not isinstance(product_id, int):
            raise LiveImportError("native edit payload requires an integer product id")
        normalized = copy.deepcopy(payload)
        normalized["id"] = product_id
        validate_canonical_update_payload(normalized)
        return self._native_save_product(
            normalized,
            confirmation=confirmation,
            operation="native staged MIKIHOUSE product update",
        )

    def _require_write_confirmation(self, confirmation: str) -> None:
        if confirmation != self.write_confirmation:
            raise LiveImportError("real Shijiu mutation blocked: exact confirmation phrase missing")

    def _download_official_image(self, source_url: str) -> tuple[bytes, str, str]:
        parsed = urllib.parse.urlparse(source_url)
        if parsed.scheme != "https" or not parsed.netloc.endswith(
            OFFICIAL_MIKIHOUSE_IMAGE_HOST_SUFFIXES
        ):
            raise ContractMismatchError(f"image source is not an official HTTPS MIKI HOUSE host: {parsed.netloc}")
        request = urllib.request.Request(
            source_url,
            headers={
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "User-Agent": "Mozilla/5.0 (compatible; mikihouse-luyao/0.8)",
            },
        )
        with urllib.request.urlopen(request, timeout=max(self.timeout, 120)) as response:
            data = response.read()
            mime_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip()
        if len(data) < 100 or not mime_type.startswith("image/"):
            raise ContractMismatchError(
                f"official image download invalid: mime={mime_type!r}, bytes={len(data)}"
            )
        filename = os.path.basename(parsed.path) or f"{uuid.uuid4().hex}.jpg"
        if "." not in filename:
            filename += mimetypes.guess_extension(mime_type) or ".jpg"
        return data, filename, mime_type

    @property
    def write_request_count(self) -> int:
        return sum(item["semantic_operation"] == "write" for item in self.requests)


def load_live_batch(
    batch_path: Path, previews_path: Path, special_path: Path
) -> tuple[list[dict[str, Any]], set[str], dict[str, Any]]:
    config = json.loads(batch_path.read_text(encoding="utf-8"))
    product_numbers = config.get("product_numbers") or []
    if (
        config.get("source") != SOURCE_CODE
        or config.get("target") != "SHIJIU"
        or config.get("fixed_target_category_id") != 294884
        or config.get("batch_size") != 20
        or len(product_numbers) != 20
        or len(set(product_numbers)) != 20
    ):
        raise LiveImportError("invalid frozen first-live-batch configuration")
    special = set(read_product_numbers(special_path))
    if len(special) != EXPECTED_SPECIAL_COUNT:
        raise LiveImportError(f"expected 351 PDF special exclusions, got {len(special)}")
    leaked = sorted(set(product_numbers) & special)
    if leaked:
        raise LiveImportError(f"{PDF_SPECIAL_EXCLUDED_REASON}: frozen live batch contains {leaked}")
    preview_document = json.loads(previews_path.read_text(encoding="utf-8"))
    by_number = {item["product_number"]: item for item in preview_document.get("payloads") or []}
    if set(by_number) != set(product_numbers):
        raise LiveImportError("frozen batch and committed 20-product payload previews differ")
    selected = [copy.deepcopy(by_number[number]) for number in product_numbers]
    for item in selected:
        if item.get("source") != SOURCE_CODE or not item.get("publish_ready"):
            raise LiveImportError(f"first live batch item is not publishable MIKIHOUSE: {item.get('product_number')}")
        if item.get("target_category", {}).get("id") != 294884:
            raise LiveImportError("first live batch item does not use fixed category 294884")
        if content_sha256(item["shijiu_payload_preview"]) != item.get("payload_sha256"):
            raise LiveImportError(f"payload preview hash mismatch: {item['product_number']}")
    return selected, special, config


def initial_checkpoint(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": LIVE_BATCH_SCHEMA_VERSION,
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "batch_name": "MIKIHOUSE_FIRST_20_REAL_IMPORT",
        "created_at": now(),
        "updated_at": now(),
        "status": "READY",
        "stop_reason": None,
        "fixed_target_category_id": 294884,
        "legacy_reference_touched": False,
        "legacy_cleanup_executed": False,
        "records": {
            item["product_number"]: {
                "source": SOURCE_CODE,
                "source_product_id": item["source_product_id"],
                "product_number": item["product_number"],
                "classification": item["classification"],
                "source_payload_sha256": item["payload_sha256"],
                "state": "PLANNED",
                "image_uploads": {},
                "create_intent_at": None,
                "create_response": None,
                "shijiu_product_id": None,
                "readback_verified_at": None,
                "readback_result": None,
                "error": None,
            }
            for item in items
        },
        "request_ledger": [],
    }


def load_or_create_checkpoint(path: Path, items: list[dict[str, Any]]) -> dict[str, Any]:
    if not path.exists():
        checkpoint = initial_checkpoint(items)
        write_json_atomic(path, checkpoint)
        return checkpoint
    checkpoint = json.loads(path.read_text(encoding="utf-8"))
    expected = [item["product_number"] for item in items]
    if (
        checkpoint.get("source") != SOURCE_CODE
        or checkpoint.get("target") != "SHIJIU"
        or checkpoint.get("fixed_target_category_id") != 294884
        or list((checkpoint.get("records") or {}).keys()) != expected
    ):
        raise LiveImportError("checkpoint does not match the frozen MIKIHOUSE first batch")
    for item in items:
        record = checkpoint["records"][item["product_number"]]
        if record.get("source_payload_sha256") != item.get("payload_sha256"):
            raise LiveImportError(f"checkpoint payload drift: {item['product_number']}")
    return checkpoint


def _save_checkpoint(path: Path, checkpoint: dict[str, Any]) -> None:
    checkpoint["updated_at"] = now()
    write_json_atomic(path, checkpoint)


def _unique_exact_product_matches(
    client: ShijiuLiveClient, sku_code: str
) -> list[dict[str, Any]]:
    """Return Goods.index rows found by good_code.

    This is intentionally only auxiliary evidence for CREATE readback. Shijiu's
    list endpoint does not reliably search backend variant ``sku_code`` through
    the ``good_code`` field, so callers must never bind a newly created product
    from this result alone.
    """
    matches: dict[str, dict[str, Any]] = {}
    for status in ("", "2", "1", "0"):
        response = client.search_products(sku_code, status=status)
        for row in response_rows(response):
            product_id = str(row.get("id") or row.get("good_id") or row.get("goods_id") or "")
            if product_id:
                matches[product_id] = row
    return list(matches.values())


def _row_product_id(row: dict[str, Any]) -> str:
    return str(row.get("id") or row.get("good_id") or row.get("goods_id") or "").strip()


def _row_good_name(row: dict[str, Any]) -> str:
    return str(row.get("good_name") or row.get("goods_name") or row.get("name") or "").strip()


def _response_count(response: dict[str, Any]) -> int | None:
    for container in (response, response.get("data")):
        if not isinstance(container, dict):
            continue
        value = container.get("count")
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    value = response.get("count")
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _unique_exact_name_product_matches(
    client: ShijiuLiveClient,
    good_name: str,
    *,
    category_id: int = 294884,
    page_size: int = 100,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Locate CREATE candidates by exact good_name, never by fuzzy matching.

    The first query mirrors the browser-exact persistence proof. An unscoped
    query is also made so a target-side category filter quirk cannot hide a
    candidate; getFormatInfo remains responsible for proving category 294884.
    """
    expected_name = str(good_name).strip()
    if not expected_name:
        raise LiveImportError("CREATE readback requires a non-empty exact good_name")
    matches: dict[str, dict[str, Any]] = {}
    queries = []
    for label, good_type in (("category_294884", category_id), ("all_categories", "")):
        first = client.search_products(
            "",
            good_name=expected_name,
            good_type=good_type,
            push="",
            status="",
            page=1,
            page_size=page_size,
        )
        declared = _response_count(first)
        pages = max(1, ((declared or 0) + page_size - 1) // page_size)
        query_rows = response_rows(first)
        for page in range(2, pages + 1):
            query_rows.extend(response_rows(client.search_products(
                "",
                good_name=expected_name,
                good_type=good_type,
                push="",
                status="",
                page=page,
                page_size=page_size,
            )))
        exact_rows = [row for row in query_rows if _row_good_name(row) == expected_name]
        for row in exact_rows:
            product_id = _row_product_id(row)
            if product_id:
                matches[product_id] = row
        queries.append({
            "label": label,
            "good_type": str(good_type),
            "declared_count": declared,
            "pages_read": pages,
            "returned_row_count": len(query_rows),
            "exact_name_match_ids": sorted({
                _row_product_id(row) for row in exact_rows if _row_product_id(row)
            }),
        })
    return list(matches.values()), {
        "primary_identity_path": "Goods.index exact good_name -> product_id -> getFormatInfo exact sku_code",
        "exact_good_name": expected_name,
        "candidate_product_ids": sorted(matches),
        "queries": queries,
    }


def verify_exact_name_create_candidates(
    client: ShijiuLiveClient,
    item: dict[str, Any],
    payload: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    create_response: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Strongly verify every exact-name candidate with getFormatInfo.

    A candidate is accepted only when the existing full readback validator
    proves product id, name, category, prices, specifications and all images.
    """
    verified: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    response_id = _product_id_from_value(create_response or {})
    for row in rows:
        product_id = _row_product_id(row)
        if not product_id:
            continue
        observation: dict[str, Any] = {
            "product_id": product_id,
            "list_good_name_exact": _row_good_name(row) == str(payload["good_name"]).strip(),
            "passed": False,
        }
        if response_id and str(response_id) != product_id:
            observation["mismatch"] = "create response product_id mismatch"
            observations.append(observation)
            continue
        detail = client.product_detail(product_id)
        try:
            readback = validate_product_readback(
                item,
                payload,
                product_id,
                detail,
                create_response=create_response,
                list_row=row,
            )
        except ContractMismatchError as error:
            observation["mismatch"] = str(error)
        else:
            observation["passed"] = True
            observation["exact_backend_sku_codes"] = [
                sku["backend_sku_code"] for sku in readback["skus"]
            ]
            verified.append({"list_row": row, "readback": readback})
        observations.append(observation)
    return verified, observations


def _resolve_payload(item: dict[str, Any], uploaded: dict[str, dict[str, Any]]) -> dict[str, Any]:
    target_by_reference = {
        reference: row.get("target_url") for reference, row in uploaded.items()
    }
    expected = {entry["upload_reference"] for entry in item["image_upload_plan"]}
    if set(target_by_reference) != expected or not all(target_by_reference.values()):
        raise LiveImportError(f"image upload set is incomplete: {item['product_number']}")

    def replace(current: Any) -> Any:
        if isinstance(current, dict):
            return {key: replace(child) for key, child in current.items()}
        if isinstance(current, list):
            return [replace(child) for child in current]
        if isinstance(current, str):
            return PLACEHOLDER_PATTERN.sub(
                lambda match: str(target_by_reference.get(match.group(1)) or match.group(0)), current
            )
        return current

    payload = replace(copy.deepcopy(item["shijiu_payload_preview"]))
    payload["state"] = "1"
    payload["is_shelf"] = 0
    serialized = canonical_json(payload)
    if "SHIJIU_COS_URL" in serialized:
        raise LiveImportError(f"unresolved COS placeholder: {item['product_number']}")
    formal_images = canonical_json({
        "master_graph": payload.get("master_graph"),
        "broadcast": payload.get("broadcast"),
        "good_detail_pics": payload.get("good_detail_pics"),
        "good_details": payload.get("good_details"),
        "sku_info": payload.get("sku_info"),
    })
    if "cdn.shopify.com" in formal_images or "mikihouse.co.jp" in formal_images:
        raise LiveImportError(f"official external image leaked into formal payload: {item['product_number']}")
    if payload.get("good_type") != 294884 or payload.get("state") != "1" or payload.get("is_shelf") != 0:
        raise LiveImportError("fixed category or off-shelf invariant failed")
    return payload


def _sku_id_from_row(row: dict[str, Any]) -> str | None:
    for key in TARGET_SKU_ID_FIELDS:
        value = row.get(key)
        if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
            return str(value).strip()
    return None


def validate_product_readback(
    item: dict[str, Any],
    payload: dict[str, Any],
    product_id: str,
    detail: dict[str, Any],
    *,
    create_response: dict[str, Any] | None = None,
    list_row: dict[str, Any] | None = None,
    expected_state: str = "1",
    require_is_shelf: bool = True,
    require_exact_good_details: bool = False,
) -> dict[str, Any]:
    _assert_success(detail, "product readback")
    detail_data = detail.get("data") if isinstance(detail.get("data"), dict) else {}
    actual_id = _product_id_from_value({"data": detail_data})
    if actual_id not in (None, "") and str(actual_id) != str(product_id):
        raise ContractMismatchError(
            f"product ID mismatch: expected {product_id}, got {actual_id}"
        )
    actual_category = _first_observation(detail, ("good_type",))
    if str(actual_category) != "294884":
        raise ContractMismatchError(f"category readback mismatch: {actual_category!r}")
    actual_name = _first_observation(detail, ("good_name", "goods_name"))
    if str(actual_name or "").strip() != str(payload["good_name"]).strip():
        raise ContractMismatchError("good_name readback mismatch")
    actual_master = str(_first_observation(detail, ("master_graph",)) or "").strip()
    if actual_master != str(payload["master_graph"]).strip():
        raise ContractMismatchError("master_graph readback mismatch")
    actual_broadcast = _split_urls(_first_observation(detail, ("broadcast",)))
    expected_broadcast = _split_urls(payload["broadcast"])
    if actual_broadcast != expected_broadcast:
        raise ContractMismatchError(
            f"broadcast readback mismatch: expected {len(expected_broadcast)}, got {len(actual_broadcast)}"
        )
    actual_details = str(_first_observation(detail, ("good_details",)) or "")
    expected_detail_urls = _split_urls(payload.get("good_detail_pics"))
    actual_detail_urls = _split_urls(_first_observation(detail, ("good_detail_pics",)))
    if actual_detail_urls != expected_detail_urls:
        raise ContractMismatchError(
            "good_detail_pics readback mismatch: "
            f"expected {len(expected_detail_urls)}, got {len(actual_detail_urls)}"
        )
    if not actual_details or any(url not in actual_details for url in expected_detail_urls):
        raise ContractMismatchError("good_details readback is empty or missing uploaded detail images")
    if require_exact_good_details and actual_details != str(payload.get("good_details") or ""):
        raise ContractMismatchError("good_details exact content/hash readback mismatch")
    actual_skus = recursively_find_skus(detail)
    by_code = {str(row.get("sku_code") or "").strip(): row for row in actual_skus}
    expected_codes = [str(row["sku_code"]) for row in payload["sku_info"]]
    if set(by_code) != set(expected_codes) or len(by_code) != len(payload["sku_info"]):
        raise ContractMismatchError(
            f"SKU code/count mismatch: expected {len(expected_codes)}, got {len(by_code)}"
        )
    create_sku_rows = {
        str(row.get("sku_code") or "").strip(): row
        for row in recursively_find_skus(create_response or {})
    }
    source_variants_by_backend_code = {
        f"MIKI-{str(row.get('source_variant_sku') or '').strip()}": row
        for row in item.get("source_variants") or []
        if str(row.get("source_variant_sku") or "").strip()
    }
    sku_results = []
    for expected in payload["sku_info"]:
        code = str(expected["sku_code"])
        actual = by_code[code]
        source_variant = source_variants_by_backend_code.get(code)
        if source_variant is None:
            raise ContractMismatchError(f"source variant missing for readback SKU: {code}")
        if _decimal(actual.get("sku_price", actual.get("price"))) != _decimal(expected["sku_price"]):
            raise ContractMismatchError(f"price readback mismatch: {code}")
        if _decimal(actual.get("sku_stock", actual.get("stock"))) != _decimal(expected["sku_stock"]):
            raise ContractMismatchError(f"stock readback mismatch: {code}")
        actual_spec = _normalized_specification(
            actual.get("spec_name") or actual.get("spec_son_name") or ""
        )
        if str(expected["spec_name"]).strip() != actual_spec:
            raise ContractMismatchError(
                f"specification readback mismatch: {code}: {actual_spec!r}"
            )
        expected_color = str(source_variant.get("color") or "").strip()
        expected_size = str(source_variant.get("size") or "").strip()
        expected_variant_spec = ",".join(
            value for value in (expected_color, expected_size) if value
        )
        explicit_color_size_available = bool(expected_color or expected_size)
        if (
            explicit_color_size_available
            and expected_variant_spec != str(expected["spec_name"]).strip()
        ):
            raise ContractMismatchError(
                f"source color/size to payload specification mismatch: {code}"
            )
        if str(actual.get("sku_thumbnail") or "").strip() != str(expected["sku_thumbnail"]).strip():
            raise ContractMismatchError(f"SKU image readback mismatch: {code}")
        target_sku_id = _sku_id_from_row(actual) or _sku_id_from_row(create_sku_rows.get(code, {}))
        sku_results.append({
            "source_variant_sku": code.removeprefix("MIKI-"),
            "backend_sku_code": code,
            "shijiu_sku_id": target_sku_id,
            "stable_target_identity": {
                "shijiu_product_id": str(product_id),
                "backend_sku_code": code,
            },
            "price_jpy": int(_decimal(expected["sku_price"])),
            "stock": int(_decimal(expected["sku_stock"])),
            "specification": expected["spec_name"],
            "color": expected_color,
            "size": expected_size,
            "color_size_verified_via_exact_specification": (
                explicit_color_size_available
                and expected_variant_spec == actual_spec
            ),
            "image_url": expected["sku_thumbnail"],
            "passed": True,
        })
    actual_state = _first_observation(detail, ("state",))
    actual_is_shelf = _first_observation(detail, ("is_shelf",))
    if list_row:
        actual_state = list_row.get("state", actual_state)
        actual_is_shelf = list_row.get("is_shelf", actual_is_shelf)
    shelf_value_exposed = actual_is_shelf not in (None, "")
    shelf_value_off = str(actual_is_shelf) in {"0", "False", "false"}
    if (
        str(actual_state) != str(expected_state)
        or (require_is_shelf and not shelf_value_off)
        or (not require_is_shelf and shelf_value_exposed and not shelf_value_off)
    ):
        raise ContractMismatchError(
            f"off-shelf readback mismatch: state={actual_state!r}, is_shelf={actual_is_shelf!r}"
        )
    return {
        "source": SOURCE_CODE,
        "source_product_id": item["source_product_id"],
        "product_number": item["product_number"],
        "shijiu_product_id": str(product_id),
        "target_category_id": 294884,
        "good_name": payload["good_name"],
        "off_shelf": True if shelf_value_off else None,
        "is_shelf_exposed": shelf_value_exposed,
        "master_graph": payload["master_graph"],
        "carousel_urls": expected_broadcast,
        "detail_image_urls": expected_detail_urls,
        "good_details_sha256": hashlib.sha256(actual_details.encode("utf-8")).hexdigest(),
        "sku_count": len(sku_results),
        "skus": sku_results,
        "passed": True,
        "verified_at": now(),
    }


def persist_verified_mapping(
    mapping_path: Path,
    item: dict[str, Any],
    readback: dict[str, Any],
    resolved_payload_sha256: str,
) -> None:
    state = load_mapping_state(mapping_path)
    row = state["products"].get(item["product_number"])
    if not row or row.get("source") != SOURCE_CODE:
        raise LiveImportError(f"missing isolated MIKIHOUSE mapping row: {item['product_number']}")
    previous_id = row.get("shijiu_product_id")
    if previous_id not in (None, "", readback["shijiu_product_id"]):
        raise DuplicateRiskError("attempt to replace an existing Shijiu product mapping")
    verified_at = readback["verified_at"]
    row.update({
        "shijiu_product_id": readback["shijiu_product_id"],
        "match_method": "post_create_detail_readback",
        "target_category_id": 294884,
        "target_active": False,
        "last_payload_sha256": resolved_payload_sha256,
        "last_verified_at": verified_at,
    })
    for sku_result in readback["skus"]:
        sku = sku_result["source_variant_sku"]
        variant = row["variants"].get(sku)
        if not variant or variant.get("source") != SOURCE_CODE:
            raise LiveImportError(f"missing isolated MIKIHOUSE variant mapping: {sku}")
        previous_sku_id = variant.get("shijiu_sku_id")
        if previous_sku_id not in (None, "", sku_result["shijiu_sku_id"]):
            raise DuplicateRiskError(f"attempt to replace existing Shijiu SKU mapping: {sku}")
        variant.update({
            "shijiu_sku_id": sku_result["shijiu_sku_id"],
            "target_product_id": readback["shijiu_product_id"],
            "backend_sku_code_verified": True,
            "match_method": "post_create_product_id_and_backend_sku_code_readback",
            "last_verified_at": verified_at,
        })
    state["updated_at"] = now()
    write_json_atomic(mapping_path, state)


def build_live_report(
    checkpoint: dict[str, Any],
    items: list[dict[str, Any]],
    client: ShijiuLiveClient,
    special: set[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    records = checkpoint["records"]
    state_counts = Counter(record["state"] for record in records.values())
    completed = [record for record in records.values() if record["state"] == "READBACK_VERIFIED"]
    image_rows = [
        image
        for record in records.values()
        for image in (record.get("image_uploads") or {}).values()
        if image.get("status") == "UPLOADED"
    ]
    create_responses = [
        record["create_response"]
        for record in records.values()
        if isinstance(record.get("create_response"), dict)
    ]
    first_create_response = create_responses[0] if create_responses else {}
    create_data = first_create_response.get("data")
    ledger = checkpoint.get("request_ledger") or client.requests
    report = {
        "schema_version": LIVE_BATCH_SCHEMA_VERSION,
        "generated_at": now(),
        "mode": "REAL_WRITE_VALIDATION",
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "status": checkpoint.get("status"),
        "stop_reason": checkpoint.get("stop_reason"),
        "requested_product_count": len(items),
        "verified_product_count": len(completed),
        "remaining_product_count": len(items) - len(completed),
        "state_counts": dict(sorted(state_counts.items())),
        "fixed_target_category_id": 294884,
        "default_visibility": "OFF_SHELF_INVISIBLE",
        "uploaded_official_image_count": len(image_rows),
        "uploaded_image_target_hosts": sorted({
            urllib.parse.urlparse(row["target_url"]).netloc for row in image_rows
        }),
        "created_product_ids": [row["shijiu_product_id"] for row in completed],
        "confirmed_created_product_count": len(completed),
        "batch_mapping_bindings_written": {
            "products": len(completed),
            "skus": sum(row["readback_result"]["sku_count"] for row in completed),
        },
        "create_endpoint_response": ({
            "code": first_create_response.get("code"),
            "msg": first_create_response.get("msg"),
            "data_shape": (
                "empty_list" if isinstance(create_data, list) and not create_data
                else type(create_data).__name__
            ),
            "product_id_exposed": bool(_product_id_from_value(first_create_response)),
            "creation_confirmed_by_exact_name_then_detail_readback": bool(completed),
        } if first_create_response else None),
        "product_identity_readback_policy": (
            "Goods.index exact good_name -> product_id -> getFormatInfo exact backend_sku_code"
        ),
        "good_code_search_role": "auxiliary_only_never_binding",
        "verified_sku_count": sum(row["readback_result"]["sku_count"] for row in completed),
        "price_source": "mini_program_price_jpy",
        "currency": "JPY",
        "currency_conversion_applied": False,
        "special_exclusion_count": len(special),
        "special_excluded_reason": PDF_SPECIAL_EXCLUDED_REASON,
        "special_product_in_batch_count": len({item["product_number"] for item in items} & special),
        "legacy_reference_touched": False,
        "legacy_cleanup_executed": False,
        "rollback": {
            "executed": False,
            "status": (
                "NOT_EXECUTABLE_WITHOUT_CONFIRMED_PRODUCT_ID"
                if checkpoint.get("status") == "STOPPED_ON_FIRST_ERROR" and not completed
                else "NOT_REQUIRED"
            ),
            "product_mutations": 0,
            "uploaded_images_retained": len(image_rows),
        },
        "subsequent_product_writes_suppressed": sum(
            record["state"] == "PLANNED" for record in records.values()
        ),
        "post_stop_read_only_forensics": (
            "deliverables/shijiu_import/first_live_batch_forensics.json"
            if checkpoint.get("status") == "STOPPED_ON_FIRST_ERROR"
            else None
        ),
        "request_counts": {
            "total": len(ledger),
            "read": sum(row["semantic_operation"] == "read" for row in ledger),
            "write": sum(row["semantic_operation"] == "write" for row in ledger),
            "image_upload": sum(row["path"] == IMAGE_UPLOAD_PATH for row in ledger),
            "product_create": sum(row["path"] == CREATE_PATH for row in ledger),
            "legacy_cleanup": 0,
        },
        "fail_closed": True,
        "resume_checkpoint": "state/shijiu_first_live_batch_checkpoint.json",
        "mapping_state": "state/shijiu_mappings.json",
        "readback_results": "deliverables/shijiu_import/first_live_batch_readbacks.json",
        "product_results": [
            {
                "product_number": record["product_number"],
                "classification": record["classification"],
                "state": record["state"],
                "shijiu_product_id": record.get("shijiu_product_id"),
                "uploaded_image_count": sum(
                    row.get("status") == "UPLOADED"
                    for row in (record.get("image_uploads") or {}).values()
                ),
                "error": record.get("error"),
            }
            for record in records.values()
        ],
    }
    readbacks = {
        "schema_version": LIVE_BATCH_SCHEMA_VERSION,
        "generated_at": now(),
        "source": SOURCE_CODE,
        "target": "SHIJIU",
        "results": [record["readback_result"] for record in completed],
        "verified_product_count": len(completed),
        "verified_sku_count": report["verified_sku_count"],
        "all_passed": len(completed) == 20 and checkpoint.get("status") == "COMPLETED",
    }
    return report, readbacks


class FirstLiveBatchRunner:
    def __init__(
        self,
        client: ShijiuLiveClient,
        items: list[dict[str, Any]],
        special: set[str],
        category: dict[str, Any],
        checkpoint_path: Path,
        mapping_path: Path,
        report_path: Path,
        readbacks_path: Path,
        *,
        confirmation: str,
    ) -> None:
        self.client = client
        self.items = items
        self.special = special
        self.category = category
        self.checkpoint_path = checkpoint_path
        self.mapping_path = mapping_path
        self.report_path = report_path
        self.readbacks_path = readbacks_path
        self.confirmation = confirmation
        self.checkpoint = load_or_create_checkpoint(checkpoint_path, items)

    def _persist(self) -> None:
        cursor = getattr(self, "_request_cursor", 0)
        self.checkpoint.setdefault("request_ledger", []).extend(
            copy.deepcopy(self.client.requests[cursor:])
        )
        self._request_cursor = len(self.client.requests)
        _save_checkpoint(self.checkpoint_path, self.checkpoint)
        report, readbacks = build_live_report(
            self.checkpoint, self.items, self.client, self.special
        )
        write_json_atomic(self.report_path, report)
        write_json_atomic(self.readbacks_path, readbacks)

    def _stop(self, record: dict[str, Any] | None, error: Exception) -> None:
        if record is not None:
            record["error"] = {"type": type(error).__name__, "message": str(error), "at": now()}
            if record["state"] not in {"CREATE_INTENT_PERSISTED", "CREATE_RESULT_UNKNOWN"}:
                record["state"] = "STOPPED_ON_ERROR"
        self.checkpoint["status"] = "STOPPED_ON_FIRST_ERROR"
        self.checkpoint["stop_reason"] = {"type": type(error).__name__, "message": str(error), "at": now()}
        self._persist()

    def _preflight(self) -> None:
        if len(self.special) != EXPECTED_SPECIAL_COUNT:
            raise LiveImportError("permanent PDF special exclusion count changed")
        batch_numbers = {item["product_number"] for item in self.items}
        if batch_numbers & self.special:
            raise LiveImportError(f"{PDF_SPECIAL_EXCLUDED_REASON}: preflight boundary failure")
        validate_live_mikihouse_category(self.category, self.client.categories())
        mapping = load_mapping_state(self.mapping_path)
        unbound_count = 0
        for item in self.items:
            row = mapping["products"][item["product_number"]]
            if row.get("shijiu_product_id") not in (None, ""):
                continue
            unbound_count += 1
            first_code = item["source_variants"][0]["backend_sku_code"]
            matches = _unique_exact_product_matches(self.client, first_code)
            if matches:
                raise DuplicateRiskError(
                    f"unmapped MIKIHOUSE identity already exists in Shijiu: {first_code}; refusing to bind or recreate"
                )
        self.checkpoint["preflight"] = {
            "passed": True,
            "at": now(),
            "category_id": 294884,
            "exact_identity_absence_checks": unbound_count,
            "legacy_reference_scanned_or_bound": False,
        }
        self._persist()

    def run(self) -> dict[str, Any]:
        if self.confirmation != LIVE_WRITE_CONFIRMATION:
            raise LiveImportError("exact real-write confirmation was not supplied")
        if self.checkpoint.get("status") == "COMPLETED":
            self._persist()
            return json.loads(self.report_path.read_text(encoding="utf-8"))
        if self.checkpoint.get("status") == "STOPPED_ON_FIRST_ERROR":
            raise LiveImportError(
                "checkpoint is frozen after a prior anomaly; inspect the report before any manually authorized resume"
            )
        try:
            self._preflight()
        except Exception as error:
            self._stop(None, error)
            raise
        mapping = load_mapping_state(self.mapping_path)
        for item in self.items:
            record = self.checkpoint["records"][item["product_number"]]
            try:
                if item["product_number"] in self.special:
                    raise LiveImportError(f"{PDF_SPECIAL_EXCLUDED_REASON}: write-stage boundary failure")
                bound_id = mapping["products"][item["product_number"]].get("shijiu_product_id")
                if record["state"] == "READBACK_VERIFIED":
                    if str(bound_id) != str(record["shijiu_product_id"]):
                        raise DuplicateRiskError("checkpoint/mapping product ID mismatch")
                    continue
                if record["state"] in {"CREATE_INTENT_PERSISTED", "CREATE_RESULT_UNKNOWN"}:
                    raise DuplicateRiskError(
                        "an earlier create result is unresolved; automatic second create is forbidden"
                    )
                if bound_id not in (None, ""):
                    detail = self.client.product_detail(bound_id)
                    payload = _resolve_payload(item, record["image_uploads"])
                    readback = validate_product_readback(item, payload, str(bound_id), detail)
                    record.update({
                        "state": "READBACK_VERIFIED",
                        "shijiu_product_id": str(bound_id),
                        "readback_verified_at": readback["verified_at"],
                        "readback_result": readback,
                        "error": None,
                    })
                    self._persist()
                    continue
                for upload in item["image_upload_plan"]:
                    reference = upload["upload_reference"]
                    existing = record["image_uploads"].get(reference)
                    if existing and existing.get("status") == "UPLOADED":
                        continue
                    if existing:
                        raise DuplicateRiskError(
                            f"image upload result is unresolved for {reference}; automatic re-upload is forbidden"
                        )
                    record["image_uploads"][reference] = {
                        "upload_reference": reference,
                        "order": upload["order"],
                        "role": upload["role"],
                        "source_url": upload["source_url"],
                        "source_url_sha256": hashlib.sha256(upload["source_url"].encode()).hexdigest(),
                        "target_url": None,
                        "status": "UPLOAD_INTENT_PERSISTED",
                        "intent_at": now(),
                    }
                    record["state"] = "UPLOADING_IMAGES"
                    self._persist()
                    try:
                        target_url, response = self.client.upload_image(
                            upload["source_url"], confirmation=self.confirmation
                        )
                    except Exception:
                        record["image_uploads"][reference]["status"] = "UPLOAD_RESULT_UNKNOWN"
                        self._persist()
                        raise
                    record["image_uploads"][reference].update({
                        "target_url": target_url,
                        "status": "UPLOADED",
                        "completed_at": now(),
                        "response": _redacted_response(response),
                    })
                    self._persist()
                record["state"] = "IMAGES_COMPLETE"
                self._persist()
                payload = _resolve_payload(item, record["image_uploads"])
                first_code = item["source_variants"][0]["backend_sku_code"]
                if _unique_exact_product_matches(self.client, first_code):
                    raise DuplicateRiskError(
                        f"target identity appeared before create intent: {first_code}"
                    )
                record.update({
                    "state": "CREATE_INTENT_PERSISTED",
                    "create_intent_at": now(),
                    "resolved_payload_sha256": content_sha256(payload),
                })
                self._persist()
                try:
                    create_response = self.client.create_product(
                        payload, confirmation=self.confirmation
                    )
                except Exception:
                    record["state"] = "CREATE_RESULT_UNKNOWN"
                    self._persist()
                    raise
                record["create_response"] = _redacted_response(create_response)
                record["state"] = "CREATE_RESPONSE_RECEIVED"
                self._persist()
                verified: list[dict[str, Any]] = []
                for delay in (0, 2, 5, 10):
                    if delay:
                        time.sleep(delay)
                    name_rows, name_evidence = _unique_exact_name_product_matches(
                        self.client, payload["good_name"]
                    )
                    verified, candidate_observations = verify_exact_name_create_candidates(
                        self.client,
                        item,
                        payload,
                        name_rows,
                        create_response=create_response,
                    )
                    auxiliary_rows = _unique_exact_product_matches(self.client, first_code)
                    record["readback_discovery"] = {
                        **name_evidence,
                        "candidate_validations": candidate_observations,
                        "verified_product_ids": [
                            row["readback"]["shijiu_product_id"] for row in verified
                        ],
                        "auxiliary_good_code_product_ids": sorted({
                            _row_product_id(row) for row in auxiliary_rows if _row_product_id(row)
                        }),
                        "good_code_role": "auxiliary_only_never_binding",
                    }
                    self._persist()
                    if len(verified) == 1:
                        break
                if len(verified) != 1:
                    raise ContractMismatchError(
                        "create endpoint returned but exact good_name -> getFormatInfo "
                        f"readback returned {len(verified)} verified product matches"
                    )
                product_id = verified[0]["readback"]["shijiu_product_id"]
                readback = verified[0]["readback"]
                record["shijiu_product_id"] = product_id
                self._persist()
                persist_verified_mapping(
                    self.mapping_path, item, readback, content_sha256(payload)
                )
                mapping = load_mapping_state(self.mapping_path)
                record.update({
                    "state": "READBACK_VERIFIED",
                    "shijiu_product_id": product_id,
                    "readback_verified_at": readback["verified_at"],
                    "readback_result": readback,
                    "error": None,
                })
                self._persist()
            except Exception as error:
                self._stop(record, error)
                raise
        self.checkpoint["status"] = "COMPLETED"
        self.checkpoint["completed_at"] = now()
        self.checkpoint["stop_reason"] = None
        self._persist()
        return json.loads(self.report_path.read_text(encoding="utf-8"))


def client_from_env(
    env_path: Path,
    observer: Callable[[dict[str, Any]], None] | None = None,
    *,
    write_confirmation: str = LIVE_WRITE_CONFIRMATION,
) -> ShijiuLiveClient:
    values = load_env_file(env_path)
    return ShijiuLiveClient(
        values.get("SHIJIU_TOKEN") or values.get("MYSHOP_TOKEN") or "",
        values.get("SHIJIU_SECRET") or values.get("MYSHOP_SECRET") or "",
        base_url=(
            values.get("SHIJIU_BASE_URL")
            or values.get("MYSHOP_BASE_URL")
            or DEFAULT_SHIJIU_BASE_URL
        ),
        cookie=values.get("SHIJIU_COOKIE") or values.get("MYSHOP_COOKIE") or "",
        write_confirmation=write_confirmation,
        request_observer=observer,
    )
