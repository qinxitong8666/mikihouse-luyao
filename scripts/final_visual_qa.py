from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageStat
from pypdf import PdfReader


FORBIDDEN_PDF_TEXT = ("0.73", "0.0435", "73折", "税入", "税込", "底价", "計算式", "计算公式")


def render_pdf(pdf_path: Path, render_dir: Path, dpi: int, pdftoppm: str) -> None:
    if dpi < 200:
        raise ValueError("final visual QA must render at 200 dpi or higher")
    render_dir.mkdir(parents=True, exist_ok=True)
    for old_page in render_dir.glob("page-*.png"):
        old_page.unlink()
    subprocess.run(
        [pdftoppm, "-png", "-r", str(dpi), str(pdf_path), str(render_dir / "page")],
        check=True,
    )


def validate_rendered_pages(
    render_dir: Path,
    page_count: int,
    dpi: int,
    page_width_points: float,
    page_height_points: float,
) -> tuple[int, int]:
    pages = sorted(render_dir.glob("page-*.png"))
    if len(pages) != page_count:
        raise RuntimeError(f"rendered page mismatch: expected {page_count}, got {len(pages)}")
    expected_size = (
        round(page_width_points / 72 * dpi),
        round(page_height_points / 72 * dpi),
    )
    first_dimensions: tuple[int, int] | None = None
    for page in pages:
        with Image.open(page) as image:
            if first_dimensions is None:
                first_dimensions = image.size
            if any(abs(actual - expected) > 2 for actual, expected in zip(image.size, expected_size)):
                raise RuntimeError(
                    f"unexpected rendered dimensions for {page.name}: {image.size}, expected {expected_size}"
                )
            gray = image.convert("L")
            statistics = ImageStat.Stat(gray)
            if statistics.extrema[0][0] > 245 or statistics.var[0] < 1:
                raise RuntimeError(f"rendered page appears blank: {page.name}")
    assert first_dimensions is not None
    return first_dimensions


def validate_pdf_contents(
    reader: PdfReader,
    products: list[dict],
    failures: list[dict],
) -> None:
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    missing_or_duplicate = [
        item["product_number"] for item in products if text.count(item["product_number"]) != 1
    ]
    if missing_or_duplicate:
        raise RuntimeError(f"successful SKUs missing or duplicated in PDF: {missing_or_duplicate}")
    failed_in_pdf = [item["product_number"] for item in failures if item["product_number"] in text]
    if failed_in_pdf:
        raise RuntimeError(f"failed SKUs leaked into PDF: {failed_in_pdf}")
    forbidden = [token for token in FORBIDDEN_PDF_TEXT if token in text]
    if forbidden:
        raise RuntimeError(f"internal pricing text leaked into PDF: {forbidden}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render every catalog page and record the completed manual visual QA"
    )
    parser.add_argument("--pdf", default="deliverables/mikihouse_2026AW_price_catalog.pdf")
    parser.add_argument("--products", default="output/production-2026aw/products.json")
    parser.add_argument("--failures", default="output/production-2026aw/failed_skus.json")
    parser.add_argument("--report", default="deliverables/production_report.json")
    parser.add_argument("--render-dir", default="tmp/pdfs/final-200dpi-pages")
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--pdftoppm", default="pdftoppm")
    parser.add_argument(
        "--use-existing-renders",
        action="store_true",
        help="validate an already completed render instead of invoking pdftoppm",
    )
    parser.add_argument(
        "--manual-review-passed",
        action="store_true",
        help="confirm that a human inspected every rendered page before the report is updated",
    )
    args = parser.parse_args()
    if not args.manual_review_passed:
        parser.error("--manual-review-passed is required after inspecting every rendered page")
    if args.dpi < 200:
        parser.error("--dpi must be at least 200")

    pdf_path = Path(args.pdf)
    products = json.loads(Path(args.products).read_text(encoding="utf-8"))
    failures = json.loads(Path(args.failures).read_text(encoding="utf-8"))
    report_path = Path(args.report)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    reader = PdfReader(pdf_path)
    page_count = len(reader.pages)
    if not reader.pages:
        raise RuntimeError("PDF has no pages")

    expected_total = len(products) + len(failures)
    if report["total_sku_count"] != expected_total:
        raise RuntimeError("report total does not match products plus failures")
    if report["successful_sku_count"] != len(products):
        raise RuntimeError("report successful count does not match products.json")
    if report["failed_sku_count"] != len(failures):
        raise RuntimeError("report failed count does not match failed_skus.json")
    if report["pdf_page_count"] != page_count:
        raise RuntimeError("report page count does not match PDF")

    validate_pdf_contents(reader, products, failures)
    render_dir = Path(args.render_dir)
    if not args.use_existing_renders:
        render_pdf(pdf_path, render_dir, args.dpi, args.pdftoppm)
    dimensions = validate_rendered_pages(
        render_dir,
        page_count,
        args.dpi,
        float(reader.pages[0].mediabox.width),
        float(reader.pages[0].mediabox.height),
    )

    pdf_bytes = pdf_path.read_bytes()
    report.update(
        {
            "total_sku_count": expected_total,
            "successful_sku_count": len(products),
            "failed_sku_count": len(failures),
            "footwear_product_count": sum(bool(item.get("is_footwear")) for item in products),
            "footwear_color_image_count": sum(
                len({variant.get("color") or "-" for variant in item["variants"]})
                for item in products
                if item.get("is_footwear")
            ),
            "pdf_page_count": page_count,
            "pdf_file_size_bytes": len(pdf_bytes),
            "pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
            "failed_sku_handling": {
                "disposition": "official_site_currently_unavailable_or_delisted",
                "retry_attempted_during_final_qa": False,
                "included_in_pdf": False,
                "retained_in_failure_report": True,
                "count": len(failures),
            },
            "visual_qa": {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "scope": "all_pages",
                "render_dpi": args.dpi,
                "rendered_page_count": page_count,
                "render_dimensions_pixels": list(dimensions),
                "checks": [
                    "footwear_collages_and_color_labels",
                    "available_sizes_and_price_display",
                    "product_names_and_product_numbers",
                    "clipping_overlap_and_page_bounds",
                    "font_readability",
                    "image_integrity",
                ],
                "layout_changes_required": False,
                "passed": True,
            },
            "visual_qa_passed": True,
        }
    )
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
