import csv
from pathlib import Path

from scripts.production_2026aw import read_and_validate_manifest


MANIFEST = Path("special_skus_2026aw.csv")


def test_manifest_is_complete_ordered_and_unique() -> None:
    rows = read_and_validate_manifest(MANIFEST)
    assert len(rows) == 351
    assert rows[0]["product_number"] == "10-1105-495"
    assert rows[-1]["product_number"] == "41-2601-686"
    assert len({row["product_number"] for row in rows}) == len(rows)
    actual_positions = {
        (int(row["source_page"]), int(row["source_row"]), int(row["source_column"])) for row in rows
    }
    expected_positions = {(1, row, column) for row in range(1, 36) for column in range(1, 6)}
    expected_positions |= {(2, row, column) for row in range(1, 33) for column in range(1, 6)}
    expected_positions |= {(2, row, column) for row in range(33, 37) for column in range(1, 5)}
    assert actual_positions == expected_positions


def test_gl_is_only_a_boolean_marker() -> None:
    with MANIFEST.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert sum(row["gold_label"] == "true" for row in rows) == 231
    assert all("GL" not in row["product_number"].upper() for row in rows)
    assert next(row for row in rows if row["product_number"] == "10-3742-791")["gold_label"] == "true"
    assert next(row for row in rows if row["product_number"] == "10-1105-495")["gold_label"] == "false"
