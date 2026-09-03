from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request

from PIL import Image

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
