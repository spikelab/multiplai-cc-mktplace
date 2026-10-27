from app.lines import line_total_cents


def basket_total_cents(basket):
    return sum(line_total_cents(sku, qty) for sku, qty in basket.items())
