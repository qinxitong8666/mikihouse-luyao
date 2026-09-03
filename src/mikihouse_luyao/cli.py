from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .csv_input import read_product_numbers
from .pdf import generate_price_list
from .scraper import ScrapeError, fetch_product, parse_product_html


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scrape MIKI HOUSE products and generate a PDF price list")
    parser.add_argument("--input", default="special_skus.csv", help="CSV containing product numbers")
    parser.add_argument("--json", default="output/products.json", help="JSON output path")
    parser.add_argument("--pdf", default="output/pdf/mikihouse_price_list.pdf", help="PDF output path")
    parser.add_argument("--html-file", help="offline HTML fixture (requires exactly one product number)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        numbers = read_product_numbers(args.input)
        if args.html_file and len(numbers) != 1:
            raise ValueError("--html-file requires exactly one product number")
        if args.html_file:
            html = Path(args.html_file).read_text(encoding="utf-8")
            products = [parse_product_html(html, numbers[0])]
        else:
            products = [fetch_product(number) for number in numbers]
        json_path = Path(args.json)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps([p.to_dict() for p in products], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        pdf_path = generate_price_list(products, args.pdf)
    except (OSError, ValueError, ScrapeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"products: {len(products)}")
    print(f"json: {json_path}")
    print(f"pdf: {pdf_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

