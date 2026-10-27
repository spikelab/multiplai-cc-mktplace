def order_items(store, order_id):
    try:
        return [item["name"] for item in store.order(str(order_id))["items"]]
    except KeyError:
        return []
