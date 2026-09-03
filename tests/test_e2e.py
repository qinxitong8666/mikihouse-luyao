import json
from dataclasses import replace
from pathlib import Path

from PIL import Image
from pypdf import PdfReader

import mikihouse_luyao.cli as cli
from mikihouse_luyao.pdf import generate_price_list
from mikihouse_luyao.scraper import ScrapeError, parse_product_html


def _product():
    fixture = Path("tests/fixtures/10-1105-495.html")
    return parse_product_html(fixture.read_text(encoding="utf-8"), "10-1105-495")


def _image(path):
    Image.new("RGB", (700, 700), "white").save(path, "JPEG")
    return path


def test_csv_to_json_failure_report_and_customer_pdf(tmp_path, monkeypatch) -> None:
    product = _product()
    image_path = _image(tmp_path / "main.jpg")
    monkeypatch.setattr(cli, "fetch_product", lambda number: product)
    monkeypatch.setattr(cli, "cache_product_image", lambda *args, **kwargs: image_path)
    csv_path = tmp_path / "special_skus.csv"
    csv_path.write_text("product_number\n10-1105-495\n", encoding="utf-8")
    json_path = tmp_path / "products.json"
    pdf_path = tmp_path / "catalog.pdf"
    failures_path = tmp_path / "failures.json"
    assert cli.main([
        "--input", str(csv_path), "--json", str(json_path), "--pdf", str(pdf_path),
        "--failures", str(failures_path), "--image-cache", str(tmp_path / "cache"), "--delay", "0",
    ]) == 0
    assert json.loads(json_path.read_text(encoding="utf-8"))[0]["tax_included_price_jpy"] == 44_000
    assert json.loads(failures_path.read_text(encoding="utf-8")) == []
    text = "".join(page.extract_text() or "" for page in PdfReader(pdf_path).pages)
    assert "人民币 ¥1,398" in text
    for forbidden in ("44,000", "44000", "0.73", "0.0435", "73折", "税入", "公式"):
        assert forbidden not in text


def test_batch_continues_and_reports_failed_sku(tmp_path, monkeypatch) -> None:
    product = _product()
    image_path = _image(tmp_path / "main.jpg")

    def fetch(number):
        if number == "99-9999-999":
            raise ScrapeError("not found")
        return product

    monkeypatch.setattr(cli, "fetch_product", fetch)
    monkeypatch.setattr(cli, "cache_product_image", lambda *args, **kwargs: image_path)
    csv_path = tmp_path / "special_skus.csv"
    csv_path.write_text("product_number\n99-9999-999\n10-1105-495\n", encoding="utf-8")
    failures_path = tmp_path / "failures.json"
    result = cli.main([
        "--input", str(csv_path), "--json", str(tmp_path / "products.json"),
        "--pdf", str(tmp_path / "catalog.pdf"), "--failures", str(failures_path),
        "--delay", "0",
    ])
    assert result == 2
    assert json.loads(failures_path.read_text(encoding="utf-8")) == [
        {"product_number": "99-9999-999", "error": "not found"}
    ]
    assert (tmp_path / "catalog.pdf").is_file()


def test_four_cards_per_page(tmp_path) -> None:
    base = _product()
    image_path = _image(tmp_path / "main.jpg")
    products = [replace(base, product_number=f"10-1105-49{i}") for i in range(5)]
    images = {product.product_number: image_path for product in products}
    pdf_path = generate_price_list(products, images, tmp_path / "five.pdf")
    assert len(PdfReader(pdf_path).pages) == 2


def test_long_name_and_multi_price_variants_fit_card(tmp_path) -> None:
    base = _product()
    varied = replace(
        base,
        name="【WEB限定】とても長い商品名を想定したミキハウスベア秋冬コレクション商品セット【配送希望日・返品不可】",
        tax_included_price_jpy=None,
        pdf_price=None,
        variants=tuple(
            replace(
                variant,
                color=color,
                size=size,
                tax_included_price_jpy=jpy,
                pdf_price=pdf_price,
            )
            for variant, color, size, jpy, pdf_price in [
                (base.variants[0], "赤", "80cm", 13_200, 420),
                (base.variants[0], "赤", "90cm", 13_200, 420),
                (base.variants[0], "紺", "100cm", 15_400, 490),
                (base.variants[0], "紺", "110cm", 15_400, 490),
                (base.variants[0], "マルチカラー", "120cm", 16_500, 524),
            ]
        ),
    )
    image_path = _image(tmp_path / "main.jpg")
    pdf_path = generate_price_list([varied], {varied.product_number: image_path}, tmp_path / "varied.pdf")
    text = PdfReader(pdf_path).pages[0].extract_text() or ""
    assert "¥420" in text and "¥490" in text and "¥524" in text
    assert "人民币 ¥420 - ¥524" in text
    assert "13200" not in text and "15400" not in text and "16500" not in text
