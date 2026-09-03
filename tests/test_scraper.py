from pathlib import Path

import pytest

from mikihouse_luyao.scraper import ScrapeError, parse_product_html


FIXTURE = Path(__file__).parent / "fixtures" / "10-1105-495.html"


def test_parses_and_validates_requested_product() -> None:
    product = parse_product_html(FIXTURE.read_text(encoding="utf-8"), "10-1105-495")
    assert product.name == "ミキハウスベア ベビーフォーマルセット"
    assert product.tax_included_price_jpy == 44_000
    assert product.pdf_price == 1_398
    assert product.main_image_url.startswith("https://www.mikihouse.co.jp/cdn/shop/products/")
    assert [(v.color, v.size, v.stock_text) for v in product.variants] == [
        ("紺", "80cm", "在庫あり"),
        ("紺", "90cm", "残り4点"),
    ]


def test_rejects_product_number_mismatch() -> None:
    with pytest.raises(ScrapeError, match="mismatch"):
        parse_product_html(FIXTURE.read_text(encoding="utf-8"), "99-9999-999")

