from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader

from mikihouse_luyao.cli import main as run_pipeline
from mikihouse_luyao.csv_input import read_product_numbers
from mikihouse_luyao.pdf import catalog_page_count_from_data
from mikihouse_luyao.pricing import calculate_pdf_price


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the real MIKI HOUSE production smoke test")
    parser.add_argument("--input", default="smoke_skus_2026aw.csv")
    parser.add_argument("--output", default="output/smoke-2026aw")
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    products_path = output / "products.json"
    failures_path = output / "failed_skus.json"
    pdf_path = output / "mikihouse_wechat_catalog.pdf"
    cache_path = output / "image-cache"
    expected = read_product_numbers(args.input)
    if len(expected) < 10:
        raise RuntimeError("smoke test requires at least 10 real product numbers")

    result = run_pipeline([
        "--input", args.input,
        "--json", str(products_path),
        "--failures", str(failures_path),
        "--pdf", str(pdf_path),
        "--image-cache", str(cache_path),
        "--delay", "0.5",
    ])
    failures = json.loads(failures_path.read_text(encoding="utf-8"))
    products = json.loads(products_path.read_text(encoding="utf-8"))
    if result != 0 or failures:
        raise RuntimeError(f"online pipeline failures: {failures}")
    if [product["product_number"] for product in products] != expected:
        raise RuntimeError("smoke output does not preserve the complete input SKU order")

    variable_price_products = []
    variant_count = 0
    for product in products:
        if not product["name"] or not product["main_image_url"] or not product["variants"]:
            raise RuntimeError(f"missing product data: {product['product_number']}")
        if not any(cache_path.glob(f"{product['product_number']}_*")):
            raise RuntimeError(f"missing cached image: {product['product_number']}")
        prices = set()
        for variant in product["variants"]:
            variant_count += 1
            for field in ("color", "size", "stock_text", "tax_included_price_jpy", "pdf_price"):
                if field not in variant:
                    raise RuntimeError(f"missing {field}: {product['product_number']}")
            expected_pdf_price = calculate_pdf_price(variant["tax_included_price_jpy"])
            if variant["pdf_price"] != expected_pdf_price:
                raise RuntimeError(f"price mismatch: {product['product_number']} / {variant['sku']}")
            prices.add(variant["tax_included_price_jpy"])
        if len(prices) > 1:
            variable_price_products.append(product["product_number"])
            if product["tax_included_price_jpy"] is not None or product["pdf_price"] is not None:
                raise RuntimeError(f"ambiguous product-level price was not cleared: {product['product_number']}")

    reader = PdfReader(pdf_path)
    if len(reader.pages) != catalog_page_count_from_data(products):
        raise RuntimeError("PDF page count does not match the card layout")
    pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
    forbidden = [token for token in ("0.73", "0.0435", "73折", "税入", "税込", "公式") if token in pdf_text]
    if forbidden:
        raise RuntimeError(f"customer PDF leaked internal pricing data: {forbidden}")
    if not variable_price_products:
        raise RuntimeError("smoke set did not exercise variable variant prices")

    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "product_count": len(products),
        "variant_count": variant_count,
        "pdf_pages": len(reader.pages),
        "failures": failures,
        "variable_price_products": variable_price_products,
        "pdf": str(pdf_path),
    }
    report_path = output / "smoke_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
