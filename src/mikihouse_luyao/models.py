from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Variant:
    color: str
    size: str
    in_stock: bool
    stock_text: str
    sku: str = ""


@dataclass(frozen=True)
class Product:
    product_number: str
    name: str
    tax_included_price_jpy: int
    pdf_price: int
    main_image_url: str
    source_url: str
    variants: tuple[Variant, ...]

    def to_dict(self) -> dict:
        return asdict(self)

