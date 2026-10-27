# Tasks

## 1. Display names

Add `app/users.py` with `display_name`, returning the user's name or
`"unknown user"`.

Interfaces:
- Produces: app.users.display_name(store: Store, user_id: int) -> str

Satisfies: Display name

## 2. Order items

Add `app/orders.py` with `order_items`, returning the item names or `[]`.

Interfaces:
- Produces: app.orders.order_items(store: Store, order_id: int) -> list[str]

Satisfies: Order items

## 3. Customer of an order

Add `app/customers.py` with `customer_of`, the display name of the order's
user, or `"unknown user"`.

Interfaces:
- Produces: app.customers.customer_of(store: Store, order_id: int) -> str
- Consumes: app.users.display_name(store: Store, user_id: int) -> str

Satisfies: Customer of an order

## 4. Order lines

Add `app/report.py` with `order_lines`, one line per existing order.

Interfaces:
- Produces: app.report.order_lines(store: Store, order_ids: list[int]) -> list[str]
- Consumes: app.customers.customer_of(store: Store, order_id: int) -> str
- Consumes: app.orders.order_items(store: Store, order_id: int) -> list[str]

Satisfies: Order lines
