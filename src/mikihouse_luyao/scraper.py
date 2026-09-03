from __future__ import annotations

import json
import re
import time
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import Product, Variant
from .pricing import calculate_pdf_price


BASE_URL = "https://www.mikihouse.co.jp/products/{product_number}"
STOREFRONT_API_URL = "https://www.mikihouse.co.jp/api/2025-07/graphql.json"
# Shopify storefront tokens are public, read-only browser credentials. The
# environment override makes token rotation deployable without a code change.
STOREFRONT_TOKEN = "b7846f73a48db7fcd6036093f8769ca2"
USER_AGENT = "Mozilla/5.0 (compatible; mikihouse-luyao/0.1; +https://github.com/qinxitong8666/mikihouse-luyao)"
STOREFRONT_QUERY = """
query ProductByHandle($handle: String!) {
  product(handle: $handle) {
    title
    handle
    featuredImage { url }
    variants(first: 100) {
      pageInfo { hasNextPage }
      nodes {
        title
        sku
        availableForSale
        price { amount currencyCode }
      }
    }
  }
}
"""


class ScrapeError(RuntimeError):
    pass


class ProductNotFoundError(ScrapeError):
    pass


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.json_ld: list[str] = []
        self.images: list[tuple[str, str]] = []
        self.text_parts: list[str] = []
        self._script_type = ""
        self._script_chunks: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script":
            self._script_type = values.get("type", "") or ""
            self._script_chunks = []
        elif tag == "img":
            src = values.get("src") or values.get("data-src") or ""
            self.images.append((values.get("alt", "") or "", src))

    def handle_data(self, data: str) -> None:
        if self._script_chunks is not None:
            self._script_chunks.append(data)
        elif data.strip():
            self.text_parts.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script_chunks is not None:
            if self._script_type == "application/ld+json":
                self.json_ld.append("".join(self._script_chunks))
            self._script_chunks = None
            self._script_type = ""


def _product_group(parser: _PageParser) -> dict:
    for raw in parser.json_ld:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("@type") == "ProductGroup":
                return candidate
    raise ScrapeError("ProductGroup JSON-LD was not found")


def _stock_labels(page_text: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    pattern = re.compile(r"(?P<size>[0-9A-Za-z.]+cm)\s*(?P<stock>在庫\s*あり|残り\s*\d+点|在庫\s*なし|売り切れ)")
    for match in pattern.finditer(page_text):
        labels.setdefault(match.group("size"), re.sub(r"\s+", "", match.group("stock")))
    return labels


def parse_product_html(html: str, requested_product_number: str, source_url: str | None = None) -> Product:
    parser = _PageParser()
    parser.feed(html)
    group = _product_group(parser)
    page_url = source_url or str(group.get("url") or BASE_URL.format(product_number=requested_product_number))
    canonical_number = page_url.rstrip("/").split("/")[-1].split("?")[0]
    if canonical_number != requested_product_number:
        raise ScrapeError(f"product number mismatch: requested {requested_product_number}, page has {canonical_number}")

    variants_raw = group.get("hasVariant") or []
    if isinstance(variants_raw, dict):
        variants_raw = [variants_raw]
    if not variants_raw:
        raise ScrapeError("no variants found")
    labels = _stock_labels("\n".join(parser.text_parts))
    product_name = str(group.get("name") or "").strip()
    parsed: list[Variant] = []
    prices: set[int] = set()
    main_image = ""
    for item in variants_raw:
        offer = item.get("offers") or {}
        variant_price = int(offer["price"])
        prices.add(variant_price)
        image = str(item.get("image") or "")
        main_image = main_image or image
        variant_name = str(item.get("name") or "")
        option = variant_name.removeprefix(product_name).lstrip(" -")
        parts = [part.strip() for part in option.split("/")]
        color, size = (parts[0], parts[1]) if len(parts) >= 2 else ("", option)
        availability = str(offer.get("availability") or "")
        in_stock = availability.rsplit("/", 1)[-1] == "InStock"
        stock_text = labels.get(size, "在庫あり" if in_stock else "在庫なし")
        parsed.append(Variant(
            color=color,
            size=size,
            in_stock=in_stock,
            stock_text=stock_text,
            tax_included_price_jpy=variant_price,
            pdf_price=calculate_pdf_price(variant_price),
            sku=str(item.get("sku") or ""),
        ))
    common_price = next(iter(prices)) if len(prices) == 1 else None
    if not product_name or not main_image:
        raise ScrapeError("required product fields are missing")
    return Product(
        product_number=requested_product_number,
        name=product_name,
        tax_included_price_jpy=common_price,
        pdf_price=calculate_pdf_price(common_price) if common_price is not None else None,
        main_image_url=main_image,
        source_url=page_url,
        variants=tuple(parsed),
    )


def parse_storefront_response(payload: dict, requested_product_number: str) -> Product:
    product = (payload.get("data") or {}).get("product")
    if not product:
        errors = "; ".join(str(item.get("message", item)) for item in payload.get("errors", []))
        if not errors and "data" in payload:
            raise ProductNotFoundError(f"product not found: {requested_product_number}")
        raise ScrapeError(errors or f"invalid Storefront response for {requested_product_number}")
    handle = str(product.get("handle") or "")
    if handle != requested_product_number:
        raise ScrapeError(f"product number mismatch: requested {requested_product_number}, API has {handle}")
    name = str(product.get("title") or "").strip()
    image = str((product.get("featuredImage") or {}).get("url") or "")
    variant_connection = product.get("variants") or {}
    if (variant_connection.get("pageInfo") or {}).get("hasNextPage"):
        raise ScrapeError("product has more than 100 variants; pagination is required")
    raw_variants = (variant_connection.get("nodes") or [])
    if not raw_variants:
        raise ScrapeError("no variants found")

    variants: list[Variant] = []
    prices: set[int] = set()
    for item in raw_variants:
        option = str(item.get("title") or "")
        parts = [part.strip() for part in option.split("/")]
        color, size = (parts[0], parts[1]) if len(parts) >= 2 else ("", option)
        price_data = item.get("price") or {}
        if price_data.get("currencyCode") != "JPY":
            raise ScrapeError(f"unexpected currency: {price_data.get('currencyCode')}")
        amount = str(price_data["amount"])
        major, dot, minor = amount.partition(".")
        if (dot and minor.strip("0")) or not major.isdigit():
            raise ScrapeError(f"JPY price must be an integer: {amount}")
        variant_price = int(major)
        prices.add(variant_price)
        in_stock = bool(item.get("availableForSale"))
        variants.append(Variant(
            color=color,
            size=size,
            in_stock=in_stock,
            stock_text="在庫あり" if in_stock else "在庫なし",
            tax_included_price_jpy=variant_price,
            pdf_price=calculate_pdf_price(variant_price),
            sku=str(item.get("sku") or ""),
        ))
    common_price = next(iter(prices)) if len(prices) == 1 else None
    if not name or not image:
        raise ScrapeError("required product fields are missing")
    return Product(
        product_number=handle,
        name=name,
        tax_included_price_jpy=common_price,
        pdf_price=calculate_pdf_price(common_price) if common_price is not None else None,
        main_image_url=image,
        source_url=BASE_URL.format(product_number=handle),
        variants=tuple(variants),
    )


def _request_with_retries(request: Request, timeout: float, retries: int) -> bytes:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read()
        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(0.5 * (2**attempt))
    raise ScrapeError(f"request failed: {last_error}") from last_error


def _fetch_storefront(product_number: str, timeout: float, retries: int) -> Product:
    import os

    body = json.dumps({"query": STOREFRONT_QUERY, "variables": {"handle": product_number}}).encode("utf-8")
    request = Request(STOREFRONT_API_URL, data=body, headers={
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "X-Shopify-Storefront-Access-Token": os.environ.get("MIKIHOUSE_STOREFRONT_TOKEN", STOREFRONT_TOKEN),
    })
    raw = _request_with_retries(request, timeout, retries)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScrapeError("Storefront API returned invalid JSON") from exc
    return parse_storefront_response(payload, product_number)


def _fetch_html(product_number: str, timeout: float, retries: int) -> Product:
    url = BASE_URL.format(product_number=product_number)
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ja-JP,ja;q=0.9"})
    html = _request_with_retries(request, timeout, retries).decode("utf-8", errors="replace")
    return parse_product_html(html, product_number, url)


def fetch_product(product_number: str, timeout: float = 20, retries: int = 2) -> Product:
    try:
        return _fetch_storefront(product_number, timeout, retries)
    except ProductNotFoundError:
        raise
    except ScrapeError as api_error:
        try:
            return _fetch_html(product_number, timeout, retries)
        except ScrapeError as html_error:
            raise ScrapeError(f"Storefront API: {api_error}; product page: {html_error}") from html_error
