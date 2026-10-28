## ADDED Requirements

Prices come from `app.catalog` (`price_of(sku)`, `skus()`). The catalogue
holds pen at 1.25, ink at 3.10 and pad at 2.005 (euros). Money is handled
in integer cents; converting a price to cents rounds half to even, as
`app.money.to_cents` does, so pad costs 200 cents.

### Requirement: Unit price
`app.pricing.unit_price_cents(sku)` SHALL return the price of one unit in
cents.

#### Scenario: Pen
- **WHEN** `unit_price_cents("pen")` is called
- **THEN** it returns `125`

### Requirement: Line total
`app.lines.line_total_cents(sku, qty)` SHALL return the unit price times
the quantity, in cents.

#### Scenario: Two inks
- **WHEN** `line_total_cents("ink", 2)` is called
- **THEN** it returns `620`

### Requirement: Basket total
`app.basket.basket_total_cents(basket)` SHALL return the sum of the line
totals of a `{sku: qty}` mapping, in cents.

#### Scenario: Basket
- **WHEN** `basket_total_cents({"pen": 2, "pad": 1})` is called
- **THEN** it returns `450`

### Requirement: Price list
`app.price_list.price_list()` SHALL return one line per catalogue item,
sorted by sku, as `"<sku>: <euros>.<cents>"` with two decimals, read
from the catalogue.

#### Scenario: List
- **WHEN** `price_list()` is called
- **THEN** it returns `["ink: 3.10", "pad: 2.00", "pen: 1.25"]`
