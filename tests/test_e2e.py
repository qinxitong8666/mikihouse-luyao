import json
from dataclasses import replace
from pathlib import Path

from PIL import Image
from pypdf import PdfReader

import mikihouse_luyao.cli as cli
from mikihouse_luyao.models import SelectedOption
from mikihouse_luyao.pdf import catalog_page_count_from_data, format_available_sizes, generate_price_list
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
    monkeypatch.setattr(cli, "cache_product_images", lambda *args, **kwargs: {"": image_path})
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
    monkeypatch.setattr(cli, "cache_product_images", lambda *args, **kwargs: {"": image_path})
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


def test_shoe_size_ranges_only_compress_when_continuous() -> None:
    assert format_available_sizes(["11.5cm", "12cm", "12.5cm", "13cm"]) == "11.5-13cm"
    assert format_available_sizes(["11.5cm", "12.5cm", "13cm"]) == "11.5/12.5/13cm"
    assert format_available_sizes(["S", "L"]) == "S/L"


def test_shoes_sort_first_and_seven_colors_use_half_page(tmp_path) -> None:
    base = _product()
    image_path = _image(tmp_path / "shoe.jpg")
    colors = ["白", "赤", "ピンク", "パープル", "アイボリー", "紺", "黄"]
    variants = []
    for index, color in enumerate(colors):
        for size_index, size in enumerate(("11.5cm", "12cm", "12.5cm")):
            variant = replace(
                base.variants[0],
                color=color,
                size=size,
                in_stock=not (color == "赤" and size == "12cm"),
                tax_included_price_jpy=13_200 + size_index * 1_100,
                pdf_price=(420, 455, 490)[size_index],
                sku=f"shoe-{index}-{size_index}",
                selected_options=(SelectedOption("カラー", color), SelectedOption("サイズ", size)),
                image_url=f"https://cdn.example/{index}.jpg",
                image_width=3000,
                image_height=3000,
            )
            variants.append(variant)
    shoe = replace(
        base,
        product_number="10-9301-495",
        name="ファーストベビーシューズ",
        variants=tuple(variants),
        is_footwear=True,
    )
    regulars = [replace(base, product_number=f"10-1105-49{i}") for i in range(3)]
    products = [*regulars, shoe]
    images = {product.product_number: image_path for product in regulars}
    images[shoe.product_number] = {color: image_path for color in colors}
    pdf_path = generate_price_list(products, images, tmp_path / "shoe-layout.pdf")
    reader = PdfReader(pdf_path)
    data = [product.to_dict() for product in products]
    assert len(reader.pages) == catalog_page_count_from_data(data) == 2
    first_page_text = reader.pages[0].extract_text() or ""
    assert first_page_text.index(shoe.product_number) < first_page_text.index(regulars[0].product_number)
    assert all(color in first_page_text for color in colors)
    assert "赤: 11.5cm" in first_page_text
    assert "12.5cm" in first_page_text
    assert "人民币 ¥420 - ¥490" in first_page_text


def test_pdf_embeds_original_shoe_image_pixels(tmp_path) -> None:
    base = _product()
    shoe = replace(
        base,
        name="ファーストベビーシューズ",
        is_footwear=True,
        variants=(replace(base.variants[0], color="白", size="12cm"),),
    )
    image_path = tmp_path / "high-resolution.jpg"
    Image.new("RGB", (3000, 3000), "white").save(image_path, "JPEG", quality=95)
    pdf_path = generate_price_list([shoe], {shoe.product_number: {"白": image_path}}, tmp_path / "sharp.pdf")
    resources = PdfReader(pdf_path).pages[0]["/Resources"]["/XObject"].get_object()
    widths = [obj.get_object()["/Width"] for obj in resources.values() if obj.get_object().get("/Subtype") == "/Image"]
    assert 3000 in widths
