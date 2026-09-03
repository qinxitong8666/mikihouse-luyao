from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image
from pypdf import PdfReader

from mikihouse_luyao.cli import main as run_pipeline
from mikihouse_luyao.pdf import catalog_page_count_from_data, format_available_sizes
from mikihouse_luyao.pricing import calculate_pdf_price


def read_expectations(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) < 10:
        raise RuntimeError("shoe smoke test requires at least 10 products")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate real multi-color MIKI HOUSE footwear")
    parser.add_argument("--input", default="shoe_smoke_skus_2026aw.csv")
    parser.add_argument("--output", default="output/shoe-smoke-2026aw")
    parser.add_argument("--delay", type=float, default=0.5)
    args = parser.parse_args()
    expectations = read_expectations(Path(args.input))
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    products_path = output / "products.json"
    failures_path = output / "failed_skus.json"
    pdf_path = output / "shoe_catalog.pdf"
    cache_path = output / "image-cache"
    result = run_pipeline([
        "--input", args.input, "--json", str(products_path), "--failures", str(failures_path),
        "--pdf", str(pdf_path), "--image-cache", str(cache_path), "--delay", str(args.delay),
    ])
    failures = json.loads(failures_path.read_text(encoding="utf-8"))
    products = json.loads(products_path.read_text(encoding="utf-8"))
    if result != 0 or failures:
        raise RuntimeError(f"shoe smoke failures: {failures}")
    if [item["product_number"] for item in products] != [row["product_number"] for row in expectations]:
        raise RuntimeError("shoe smoke output order mismatch")

    checked_colors = 0
    checked_variants = 0
    image_dimension_counts: dict[str, int] = {}
    verified_products = []
    for product, expectation in zip(products, expectations):
        if not product["is_footwear"]:
            raise RuntimeError(f"not classified as footwear: {product['product_number']}")
        colors: dict[str, set[str]] = {}
        color_images: dict[str, str] = {}
        color_dimensions: dict[str, tuple[int, int]] = {}
        for variant in product["variants"]:
            checked_variants += 1
            selected = {item["name"]: item["value"] for item in variant["selected_options"]}
            if selected.get("カラー") != variant["color"] or selected.get("サイズ") != variant["size"]:
                raise RuntimeError(f"selectedOptions mismatch: {product['product_number']} / {variant['sku']}")
            if not variant["sku"] or variant["available_for_sale"] != variant["in_stock"]:
                raise RuntimeError(f"SKU/availability mismatch: {product['product_number']}")
            if variant["pdf_price"] != calculate_pdf_price(variant["tax_included_price_jpy"]):
                raise RuntimeError(f"price mismatch: {product['product_number']} / {variant['sku']}")
            color = variant["color"]
            image_url = variant["image_url"]
            if not image_url or variant["image_width"] is None or variant["image_height"] is None:
                raise RuntimeError(f"missing variant image metadata: {product['product_number']} / {color}")
            if color in color_images and color_images[color] != image_url:
                raise RuntimeError(f"color maps to multiple images: {product['product_number']} / {color}")
            color_images[color] = image_url
            dimensions = (variant["image_width"], variant["image_height"])
            if color in color_dimensions and color_dimensions[color] != dimensions:
                raise RuntimeError(f"color image dimensions disagree: {product['product_number']} / {color}")
            color_dimensions[color] = dimensions
            if variant["in_stock"]:
                colors.setdefault(color, set()).add(variant["size"])
        if len(color_images) < int(expectation["expected_min_colors"]):
            raise RuntimeError(f"too few colors: {product['product_number']}")
        checked_colors += len(color_images)
        for color, image_url in color_images.items():
            digest = hashlib.sha256(image_url.encode("utf-8")).hexdigest()[:12]
            candidates = list(cache_path.glob(f"{product['product_number']}_{digest}.*"))
            if len(candidates) != 1:
                raise RuntimeError(f"wrong cached image count: {product['product_number']} / {color}")
            with Image.open(candidates[0]) as image:
                cached_dimensions = image.size
            if cached_dimensions != color_dimensions[color]:
                raise RuntimeError(f"cached original dimensions mismatch: {product['product_number']} / {color}")
            dimension_key = f"{cached_dimensions[0]}x{cached_dimensions[1]}"
            image_dimension_counts[dimension_key] = image_dimension_counts.get(dimension_key, 0) + 1
        for sizes in colors.values():
            format_available_sizes(list(sizes))
        verified_products.append({
            "product_number": product["product_number"],
            "name": product["name"],
            "source_url": product["source_url"],
            "colors": [
                {
                    "name": color,
                    "image_url": image_url,
                    "image_dimensions": list(color_dimensions[color]),
                    "available_variants": [
                        {
                            "size": variant["size"],
                            "sku": variant["sku"],
                            "tax_included_price_jpy": variant["tax_included_price_jpy"],
                            "pdf_price": variant["pdf_price"],
                        }
                        for variant in product["variants"]
                        if variant["color"] == color and variant["available_for_sale"]
                    ],
                }
                for color, image_url in color_images.items()
            ],
        })

    reader = PdfReader(pdf_path)
    expected_pages = catalog_page_count_from_data(products)
    if len(reader.pages) != expected_pages:
        raise RuntimeError(f"shoe PDF pages: expected {expected_pages}, got {len(reader.pages)}")
    pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
    for product in products:
        if product["product_number"] not in pdf_text:
            raise RuntimeError(f"missing product in PDF: {product['product_number']}")
        for color in dict.fromkeys(variant["color"] for variant in product["variants"]):
            if color not in pdf_text:
                raise RuntimeError(f"missing color in PDF: {product['product_number']} / {color}")
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "product_count": len(products),
        "color_count": checked_colors,
        "variant_count": checked_variants,
        "official_image_dimension_counts": image_dimension_counts,
        "pdf_page_count": len(reader.pages),
        "failures": failures,
        "verified_products": verified_products,
    }
    report_path = output / "shoe_smoke_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
