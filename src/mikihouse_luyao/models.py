from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Variant:
    color: str
    size: str
    in_stock: bool
    stock_text: str
    tax_included_price_jpy: int
    pdf_price: int
    sku: str = ""


@dataclass(frozen=True)
class Product:
    product_number: str
    name: str
    tax_included_price_jpy: int | None
    pdf_price: int | None
    main_image_url: str
    source_url: str
    variants: tuple[Variant, ...]

    def to_dict(self) -> dict:
        data = asdict(self)
        jpy_prices = [variant.tax_included_price_jpy for variant in self.variants]
        pdf_prices = [variant.pdf_price for variant in self.variants]
        data["tax_included_price_jpy_min"] = min(jpy_prices)
        data["tax_included_price_jpy_max"] = max(jpy_prices)
        data["pdf_price_min"] = min(pdf_prices)
        data["pdf_price_max"] = max(pdf_prices)
        return data
