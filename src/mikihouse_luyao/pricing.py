from decimal import Decimal, ROUND_CEILING


DISCOUNT_RATE = Decimal("0.73")
JPY_TO_PDF_RATE = Decimal("0.0435")


def calculate_pdf_price(tax_included_price_jpy: int) -> int:
    """Return ceil(tax-included JPY price * 0.73 * 0.0435)."""
    if tax_included_price_jpy < 0:
        raise ValueError("price must not be negative")
    value = Decimal(tax_included_price_jpy) * DISCOUNT_RATE * JPY_TO_PDF_RATE
    return int(value.to_integral_value(rounding=ROUND_CEILING))

