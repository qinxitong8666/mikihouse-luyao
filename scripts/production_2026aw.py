from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader

from mikihouse_luyao.cli import main as run_pipeline


SKU_PATTERN = re.compile(r"^\d{2}-\d{4}-\d{3}$")
MANIFEST_FIELDS = (
    "product_number",
    "gold_label",
    "source_page",
    "source_row",
    "source_column",
    "source_image",
)
REVIEW_FIELDS = ("raw_text", "reason", "source_page", "source_row", "source_column", "source_image")


def read_and_validate_manifest(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != MANIFEST_FIELDS:
            raise ValueError(f"manifest columns must be: {', '.join(MANIFEST_FIELDS)}")
        rows = list(reader)
    if not rows:
        raise ValueError("manifest is empty")
    seen_skus: set[str] = set()
    seen_positions: set[tuple[int, int, int]] = set()
    previous_position = (0, 0, 0)
    for row in rows:
        sku = row["product_number"]
        if not SKU_PATTERN.fullmatch(sku):
            raise ValueError(f"invalid product number: {sku}")
        if "GL" in sku.upper():
            raise ValueError(f"GL leaked into product_number: {sku}")
        if row["gold_label"] not in {"true", "false"}:
            raise ValueError(f"invalid gold_label for {sku}: {row['gold_label']}")
        if sku in seen_skus:
            raise ValueError(f"duplicate product number: {sku}")
        position = (int(row["source_page"]), int(row["source_row"]), int(row["source_column"]))
        if position in seen_positions:
            raise ValueError(f"duplicate source position: {position}")
        if position <= previous_position:
            raise ValueError(f"manifest is not in source order at {sku}: {position}")
        seen_skus.add(sku)
        seen_positions.add(position)
        previous_position = position
    return rows


def write_empty_review_report(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        csv.DictWriter(stream, fieldnames=REVIEW_FIELDS).writeheader()


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the reviewed 2026AW MIKI HOUSE catalog")
    parser.add_argument("--input", default="special_skus_2026aw.csv")
    parser.add_argument("--work-dir", default="output/production-2026aw")
    parser.add_argument("--deliverable", default="deliverables/mikihouse_2026AW_price_catalog.pdf")
    parser.add_argument("--report", default="deliverables/production_report.json")
    parser.add_argument("--delay", type=float, default=0.25)
    args = parser.parse_args()

    manifest_path = Path(args.input)
    manifest = read_and_validate_manifest(manifest_path)
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    products_path = work_dir / "products.json"
    failures_path = work_dir / "failed_skus.json"
    review_path = work_dir / "review_required.csv"
    cache_path = work_dir / "image-cache"
    working_pdf = work_dir / "mikihouse_2026AW_price_catalog.pdf"
    write_empty_review_report(review_path)

    pipeline_code = run_pipeline([
        "--input", str(manifest_path),
        "--json", str(products_path),
        "--failures", str(failures_path),
        "--pdf", str(working_pdf),
        "--image-cache", str(cache_path),
        "--delay", str(args.delay),
    ])
    if pipeline_code == 1:
        raise RuntimeError("production pipeline failed before a PDF could be generated")

    products = json.loads(products_path.read_text(encoding="utf-8"))
    failures = json.loads(failures_path.read_text(encoding="utf-8"))
    manifest_by_sku = {row["product_number"]: row for row in manifest}
    failed_skus: set[str] = set()
    enriched_failures = []
    for failure in failures:
        sku = failure["product_number"]
        if sku not in manifest_by_sku:
            raise RuntimeError(f"failure report contains an unknown SKU: {sku}")
        if sku in failed_skus:
            raise RuntimeError(f"failure report contains a duplicate SKU: {sku}")
        failed_skus.add(sku)
        source = manifest_by_sku[sku]
        enriched_failures.append({
            **failure,
            "gold_label": source["gold_label"] == "true",
            "source_page": int(source["source_page"]),
            "source_row": int(source["source_row"]),
            "source_column": int(source["source_column"]),
            "source_image": source["source_image"],
        })
    failures_path.write_text(
        json.dumps(enriched_failures, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    product_skus = [item["product_number"] for item in products]
    if len(product_skus) != len(set(product_skus)):
        raise RuntimeError("products.json contains duplicate SKUs")
    unknown_products = set(product_skus) - set(manifest_by_sku)
    if unknown_products:
        raise RuntimeError(f"products.json contains unknown SKUs: {sorted(unknown_products)}")
    successful_products = [item for item in products if item["product_number"] not in failed_skus]
    successful_skus = {item["product_number"] for item in successful_products}
    expected_successful_skus = set(manifest_by_sku) - failed_skus
    if successful_skus != expected_successful_skus:
        missing = sorted(expected_successful_skus - successful_skus)
        extra = sorted(successful_skus - expected_successful_skus)
        raise RuntimeError(f"products/failures do not cover the manifest; missing={missing}, extra={extra}")
    products_path.write_text(
        json.dumps(successful_products, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    success_count = len(successful_products)
    if success_count <= 0:
        raise RuntimeError("no successful products")
    pdf_pages = len(PdfReader(working_pdf).pages)
    expected_pages = math.ceil(success_count / 4)
    if pdf_pages != expected_pages:
        raise RuntimeError(f"PDF page mismatch: expected {expected_pages}, got {pdf_pages}")
    deliverable = Path(args.deliverable)
    deliverable.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(working_pdf, deliverable)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_manifest": str(manifest_path),
        "total_sku_count": len(manifest),
        "successful_sku_count": success_count,
        "failed_sku_count": len(failed_skus),
        "review_required_count": 0,
        "gold_label_count": sum(row["gold_label"] == "true" for row in manifest),
        "products_json_count": len(successful_products),
        "pdf_page_count": pdf_pages,
        "pipeline_exit_code": pipeline_code,
        "outputs": {
            "pdf": str(deliverable),
            "products_json": str(products_path),
            "failed_skus": str(failures_path),
            "review_required": str(review_path),
        },
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
