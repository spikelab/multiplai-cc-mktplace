from app.catalog import price_of
from app.money import to_cents


def unit_price_cents(sku):
    return to_cents(price_of(sku).split()[0])
