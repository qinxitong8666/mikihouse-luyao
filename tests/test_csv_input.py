from mikihouse_luyao.csv_input import read_product_numbers


def test_reads_named_column_and_deduplicates(tmp_path) -> None:
    path = tmp_path / "skus.csv"
    path.write_text("note,品番\na,10-1105-495\nb,10-1105-495\n", encoding="utf-8")
    assert read_product_numbers(path) == ["10-1105-495"]

