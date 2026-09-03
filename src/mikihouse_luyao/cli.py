from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .csv_input import read_product_numbers
from .image_cache import cache_product_images
from .pdf import generate_price_list
from .scraper import ScrapeError, fetch_product, parse_product_html


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scrape MIKI HOUSE products and generate a PDF price list")
    parser.add_argument("--input", default="special_skus.csv", help="CSV containing product numbers")
    parser.add_argument("--json", default="output/products.json", help="JSON output path")
    parser.add_argument("--pdf", default="output/pdf/mikihouse_wechat_catalog.pdf", help="PDF output path")
    parser.add_argument("--failures", default="output/failed_skus.json", help="failed SKU report path")
    parser.add_argument("--image-cache", default="output/image-cache", help="downloaded image cache directory")
    parser.add_argument("--delay", type=float, default=0.5, help="seconds between online product requests")
    parser.add_argument("--html-file", help="offline HTML fixture (requires exactly one product number)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        numbers = read_product_numbers(args.input)
        if args.delay < 0:
            raise ValueError("--delay must not be negative")
        if args.html_file and len(numbers) != 1:
            raise ValueError("--html-file requires exactly one product number")
        products = []
        failures = []
        for index, number in enumerate(numbers):
            try:
                if args.html_file:
                    html = Path(args.html_file).read_text(encoding="utf-8")
                    product = parse_product_html(html, number)
                else:
                    product = fetch_product(number)
                products.append(product)
            except (OSError, ValueError, ScrapeError) as exc:
                failures.append({"product_number": number, "error": str(exc)})
            if not args.html_file and index < len(numbers) - 1 and args.delay:
                time.sleep(args.delay)
        json_path = Path(args.json)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps([p.to_dict() for p in products], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        failure_path = Path(args.failures)
        failure_path.parent.mkdir(parents=True, exist_ok=True)
        failure_path.write_text(json.dumps(failures, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if not products:
            raise ScrapeError("all products failed; PDF was not generated")
        image_paths = {}
        ready_products = []
        for product in products:
            try:
                image_paths[product.product_number] = cache_product_images(product, args.image_cache)
                ready_products.append(product)
            except (OSError, ValueError, ScrapeError) as exc:
                failures.append({"product_number": product.product_number, "error": f"image: {exc}"})
        failure_path.write_text(json.dumps(failures, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if not ready_products:
            raise ScrapeError("all product images failed; PDF was not generated")
        pdf_path = generate_price_list(ready_products, image_paths, args.pdf)
    except (OSError, ValueError, ScrapeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"products: {len(products)}")
    print(f"failures: {len(failures)} ({failure_path})")
    print(f"json: {json_path}")
    print(f"pdf: {pdf_path}")
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
