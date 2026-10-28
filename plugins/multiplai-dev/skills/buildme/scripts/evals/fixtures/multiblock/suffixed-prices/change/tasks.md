# Tasks

## 1. Unit price

Add `app/pricing.py` with `unit_price_cents`, the catalogue price of one
unit in cents.

Interfaces:
- Produces: app.pricing.unit_price_cents(sku: str) -> int

Satisfies: Unit price

## 2. Line total

Add `app/lines.py` with `line_total_cents`.

Interfaces:
- Produces: app.lines.line_total_cents(sku: str, qty: int) -> int
- Consumes: app.pricing.unit_price_cents(sku: str) -> int

Satisfies: Line total

## 3. Basket total

Add `app/basket.py` with `basket_total_cents`.

Interfaces:
- Produces: app.basket.basket_total_cents(basket: dict[str, int]) -> int
- Consumes: app.lines.line_total_cents(sku: str, qty: int) -> int

Satisfies: Basket total

## 4. Price list

Add `app/price_list.py` with `price_list`, every catalogue item sorted by
sku with its price formatted from cents.

Interfaces:
- Produces: app.price_list.price_list() -> list[str]

Satisfies: Price list
