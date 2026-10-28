from app.users import display_name


def customer_of(store, order_id):
    try:
        order = store.order(str(order_id))
    except KeyError:
        return "unknown user"
    return display_name(store, order["user"])
