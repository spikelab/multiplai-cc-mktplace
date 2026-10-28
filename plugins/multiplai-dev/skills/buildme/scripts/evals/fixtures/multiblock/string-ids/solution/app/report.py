from app.customers import customer_of
from app.orders import order_items


def order_lines(store, order_ids):
    lines = []
    for order_id in order_ids:
        try:
            store.order(str(order_id))
        except KeyError:
            continue
        count = len(order_items(store, order_id))
        lines.append(f"#{order_id} {customer_of(store, order_id)}: {count} items")
    return lines
