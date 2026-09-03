import pytest

from mikihouse_luyao.pricing import calculate_pdf_price


def test_requested_price_formula() -> None:
    assert calculate_pdf_price(44_000) == 1_398


def test_negative_price_is_rejected() -> None:
    with pytest.raises(ValueError):
        calculate_pdf_price(-1)

