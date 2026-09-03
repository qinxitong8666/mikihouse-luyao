from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class SelectedOption:
    name: str
    value: str


@dataclass(frozen=True)
class Variant:
    color: str
    size: str
    in_stock: bool
    stock_text: str
    tax_included_price_jpy: int
    pdf_price: int
    sku: str = ""
    selected_options: tuple[SelectedOption, ...] = ()
    image_url: str = ""
    image_width: int | None = None
    image_height: int | None = None


@dataclass(frozen=True)
class Product:
    product_number: str
    name: str
    tax_included_price_jpy: int | None
    pdf_price: int | None
    main_image_url: str
    source_url: str
    variants: tuple[Variant, ...]
    tags: tuple[str, ...] = ()
    product_type: str = ""
    is_footwear: bool = False

    def to_dict(self) -> dict:
        data = asdict(self)
        for variant in data["variants"]:
            variant["available_for_sale"] = variant["in_stock"]
        jpy_prices = [variant.tax_included_price_jpy for variant in self.variants]
        pdf_prices = [variant.pdf_price for variant in self.variants]
        data["tax_included_price_jpy_min"] = min(jpy_prices)
        data["tax_included_price_jpy_max"] = max(jpy_prices)
        data["pdf_price_min"] = min(pdf_prices)
        data["pdf_price_max"] = max(pdf_prices)
        return data
