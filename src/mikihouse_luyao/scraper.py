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
USER_AGENT = "Mozilla/5.0 (compatible; mikihouse-luyao/0.1; +https://github.com/qinxitong8666/mikihouse-luyao)"


class ScrapeError(RuntimeError):
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
        prices.add(int(offer["price"]))
        image = str(item.get("image") or "")
        main_image = main_image or image
        variant_name = str(item.get("name") or "")
        option = variant_name.removeprefix(product_name).lstrip(" -")
        parts = [part.strip() for part in option.split("/")]
        color, size = (parts[0], parts[1]) if len(parts) >= 2 else ("", option)
        availability = str(offer.get("availability") or "")
        in_stock = availability.rsplit("/", 1)[-1] == "InStock"
        stock_text = labels.get(size, "在庫あり" if in_stock else "在庫なし")
        parsed.append(Variant(color=color, size=size, in_stock=in_stock, stock_text=stock_text, sku=str(item.get("sku") or "")))
    if len(prices) != 1:
        raise ScrapeError(f"variants have inconsistent prices: {sorted(prices)}")
    price = prices.pop()
    if not all((product_name, main_image, price >= 0)):
        raise ScrapeError("required product fields are missing")
    return Product(
        product_number=requested_product_number,
        name=product_name,
        tax_included_price_jpy=price,
        pdf_price=calculate_pdf_price(price),
        main_image_url=main_image,
        source_url=page_url,
        variants=tuple(parsed),
    )


def fetch_product(product_number: str, timeout: float = 20, retries: int = 2) -> Product:
    url = BASE_URL.format(product_number=product_number)
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ja-JP,ja;q=0.9"})
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                html = response.read().decode(charset, errors="replace")
            return parse_product_html(html, product_number, url)
        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(0.5 * (2**attempt))
    raise ScrapeError(f"failed to fetch {url}: {last_error}") from last_error

