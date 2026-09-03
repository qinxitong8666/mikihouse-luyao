from __future__ import annotations

import csv
import re
from pathlib import Path


SKU_PATTERN = re.compile(r"^\d{2}-\d{4}-\d{3}$")
SUPPORTED_HEADERS = ("product_number", "sku", "品番", "商品番号")


def read_product_numbers(path: str | Path) -> list[str]:
    csv_path = Path(path)
    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream))
    if not rows:
        raise ValueError(f"CSV is empty: {csv_path}")

    first = [cell.strip() for cell in rows[0]]
    header_index = next((first.index(h) for h in SUPPORTED_HEADERS if h in first), None)
    data_rows = rows[1:] if header_index is not None else rows
    index = header_index if header_index is not None else 0
    values = [row[index].strip() for row in data_rows if len(row) > index and row[index].strip()]
    invalid = [value for value in values if not SKU_PATTERN.fullmatch(value)]
    if invalid:
        raise ValueError(f"invalid product number(s): {', '.join(invalid)}")
    if not values:
        raise ValueError(f"no product numbers found in {csv_path}")
    return list(dict.fromkeys(values))

