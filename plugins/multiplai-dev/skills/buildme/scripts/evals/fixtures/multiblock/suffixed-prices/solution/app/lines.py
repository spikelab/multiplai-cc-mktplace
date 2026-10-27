from app.pricing import unit_price_cents


def line_total_cents(sku, qty):
    return unit_price_cents(sku) * qty
