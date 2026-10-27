## ADDED Requirements

Records come from `app.store.Store`. User ids and order ids are integers.
The shipped data holds users 1 (Ada) and 2 (Grace), and orders 10 (user 1:
pen, ink), 11 (user 3, who does not exist: pad) and 12 (user 2: no items).

### Requirement: Display name
`app.users.display_name(store, user_id)` SHALL return the user's name, or
`"unknown user"` when there is no such user.

#### Scenario: Known user
- **WHEN** `display_name(Store(), 1)` is called
- **THEN** it returns `"Ada"`

#### Scenario: Unknown user
- **WHEN** `display_name(Store(), 99)` is called
- **THEN** it returns `"unknown user"`

### Requirement: Order items
`app.orders.order_items(store, order_id)` SHALL return the names of the
order's items in order, or `[]` when there is no such order.

#### Scenario: Items
- **WHEN** `order_items(Store(), 10)` is called
- **THEN** it returns `["pen", "ink"]`

### Requirement: Customer of an order
`app.customers.customer_of(store, order_id)` SHALL return the display name
of the user who placed the order, or `"unknown user"` when the order or its
user does not exist.

#### Scenario: Customer
- **WHEN** `customer_of(Store(), 12)` is called
- **THEN** it returns `"Grace"`

### Requirement: Order lines
`app.report.order_lines(store, order_ids)` SHALL return one line per
existing order, in the given order, as `"#<id> <customer>: <n> items"`,
skipping ids with no order.

#### Scenario: Lines
- **WHEN** `order_lines(Store(), [12, 99, 10])` is called
- **THEN** it returns `["#12 Grace: 0 items", "#10 Ada: 2 items"]`
