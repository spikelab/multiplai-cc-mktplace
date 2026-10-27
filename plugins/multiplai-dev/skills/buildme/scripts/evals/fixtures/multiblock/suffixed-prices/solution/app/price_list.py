from app.catalog import skus
from app.pricing import unit_price_cents


def price_list():
    lines = []
    for sku in sorted(skus()):
        cents = unit_price_cents(sku)
        lines.append(f"{sku}: {cents // 100}.{cents % 100:02d}")
    return lines
