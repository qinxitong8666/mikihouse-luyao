from __future__ import annotations

import hashlib
import io
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request

from PIL import Image, ImageOps

from .scraper import USER_AGENT, _request_with_retries


class DailyQuoteImageError(RuntimeError):
    pass


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Identical images share a content-addressed target. Concurrent first-run
    # workers must not share its temporary filename (one replace could remove
    # another worker's pending file).
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", suffix=".part", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(data)
            handle.flush()
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def download_main_image(
    url: str, *, cache_dir: Path, timeout: float = 30, retries: int = 2
) -> dict[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise DailyQuoteImageError(f"main image must use HTTPS: {url}")
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
    metadata_path = cache_dir / "source" / f"{url_hash}.json"
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        source_path = cache_dir / "source" / metadata["filename"]
        if source_path.exists() and hashlib.sha256(source_path.read_bytes()).hexdigest() == metadata["content_sha256"]:
            return {**metadata, "source_path": str(source_path)}
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/*"})
    try:
        data = _request_with_retries(request, timeout, retries)
    except Exception as exc:
        raise DailyQuoteImageError(f"image download failed: {url}: {exc}") from exc
    content_hash = hashlib.sha256(data).hexdigest()
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            width, height = image.size
            image_format = str(image.format or "").upper()
    except OSError as exc:
        raise DailyQuoteImageError(f"image decode failed: {url}") from exc
    extension = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(image_format, ".img")
    filename = f"{content_hash}{extension}"
    source_path = cache_dir / "source" / filename
    if not source_path.exists():
        _atomic_write(source_path, data)
    metadata = {
        "source_url": url,
        "source_url_sha256": url_hash,
        "content_sha256": content_hash,
        "source_byte_count": len(data),
        "width": width,
        "height": height,
        "format": image_format,
        "filename": filename,
    }
    _atomic_write(metadata_path, (json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n").encode())
    return {**metadata, "source_path": str(source_path)}


def create_thumbnail(
    source: dict[str, Any], *, cache_dir: Path, long_edge_px: int = 360, jpeg_quality: int = 70
) -> dict[str, Any]:
    if long_edge_px < 200 or not 40 <= jpeg_quality <= 95:
        raise ValueError("invalid thumbnail settings")
    key = hashlib.sha256(
        f"{source['content_sha256']}|{long_edge_px}|{jpeg_quality}|sRGB-white-v1".encode()
    ).hexdigest()
    target = cache_dir / "thumbnails" / f"{key}.jpg"
    if not target.exists():
        with Image.open(source["source_path"]) as image:
            image.load()
            if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                rgba = image.convert("RGBA")
                flattened = Image.new("RGB", rgba.size, "white")
                flattened.paste(rgba, mask=rgba.getchannel("A"))
                image = flattened
            else:
                image = image.convert("RGB")
            image = ImageOps.exif_transpose(image)
            image.thumbnail((long_edge_px, long_edge_px), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            image.save(
                buffer,
                format="JPEG",
                quality=jpeg_quality,
                optimize=True,
                progressive=True,
                subsampling="4:2:0",
                exif=b"",
                icc_profile=None,
            )
        _atomic_write(target, buffer.getvalue())
    data = target.read_bytes()
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
        mode = image.mode
    if mode != "RGB":
        raise DailyQuoteImageError("thumbnail is not flattened RGB")
    return {
        "thumbnail_path": str(target),
        "thumbnail_sha256": hashlib.sha256(data).hexdigest(),
        "thumbnail_byte_count": len(data),
        "thumbnail_width": width,
        "thumbnail_height": height,
        "source_content_sha256": source["content_sha256"],
    }


def prepare_product_thumbnails(
    products: list[dict[str, Any]],
    *,
    cache_dir: Path,
    long_edge_px: int = 360,
    jpeg_quality: int = 70,
    workers: int = 12,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    results: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, str]] = []

    def work(product: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        number = str(product["product_number"])
        url = str((product.get("main_image") or {}).get("url") or "")
        source = download_main_image(url, cache_dir=cache_dir)
        thumb = create_thumbnail(
            source, cache_dir=cache_dir, long_edge_px=long_edge_px, jpeg_quality=jpeg_quality
        )
        return number, {**source, **thumb}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(work, product): product["product_number"] for product in products}
        for future in as_completed(futures):
            number = str(futures[future])
            try:
                product_number, result = future.result()
                results[product_number] = result
            except Exception as exc:
                failures.append({"product_number": number, "stage": "MAIN_IMAGE_PREFLIGHT", "error": str(exc)})
    failures.sort(key=lambda row: row["product_number"])
    return results, failures
