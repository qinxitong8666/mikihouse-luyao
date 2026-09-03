from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request

from PIL import Image

from .models import Product
from .scraper import USER_AGENT, _request_with_retries


def cache_product_image(
    image_url: str,
    product_number: str,
    cache_dir: str | Path,
    timeout: float = 30,
    retries: int = 2,
) -> Path:
    directory = Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    suffix = Path(urlparse(image_url).path).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        suffix = ".img"
    digest = hashlib.sha256(image_url.encode("utf-8")).hexdigest()[:12]
    target = directory / f"{product_number}_{digest}{suffix}"
    if target.is_file() and target.stat().st_size:
        try:
            with Image.open(target) as image:
                image.verify()
            return target
        except OSError:
            target.unlink(missing_ok=True)

    request = Request(image_url, headers={"User-Agent": USER_AGENT})
    data = _request_with_retries(request, timeout, retries)
    temporary = target.with_suffix(target.suffix + ".part")
    temporary.write_bytes(data)
    try:
        with Image.open(temporary) as image:
            image.verify()
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ValueError(f"invalid product image for {product_number}") from exc
    temporary.replace(target)
    return target


def cache_product_images(product: Product, cache_dir: str | Path) -> dict[str, Path]:
    if not product.is_footwear:
        return {"": cache_product_image(product.main_image_url, product.product_number, cache_dir)}

    color_urls: dict[str, str] = {}
    for variant in product.variants:
        color = variant.color or "-"
        if not variant.image_url:
            raise ValueError(f"missing footwear image for {product.product_number} / {color}")
        previous = color_urls.setdefault(color, variant.image_url)
        if previous != variant.image_url:
            raise ValueError(f"multiple footwear images for one color: {product.product_number} / {color}")
    if not color_urls:
        raise ValueError(f"no footwear colors for {product.product_number}")
    return {
        color: cache_product_image(url, product.product_number, cache_dir)
        for color, url in color_urls.items()
    }
