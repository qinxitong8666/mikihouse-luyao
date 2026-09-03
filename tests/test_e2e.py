import json

from mikihouse_luyao.cli import main


def test_csv_to_json_and_pdf(tmp_path) -> None:
    csv_path = tmp_path / "special_skus.csv"
    csv_path.write_text("product_number\n10-1105-495\n", encoding="utf-8")
    json_path = tmp_path / "products.json"
    pdf_path = tmp_path / "price-list.pdf"
    assert main(["--input", str(csv_path), "--html-file", "tests/fixtures/10-1105-495.html", "--json", str(json_path), "--pdf", str(pdf_path)]) == 0
    assert json.loads(json_path.read_text(encoding="utf-8"))[0]["pdf_price"] == 1398
    assert pdf_path.read_bytes().startswith(b"%PDF")

