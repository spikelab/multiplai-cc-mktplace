## ADDED Requirements

Events go through `app.events` (`on(name)` registers a handler,
`emit(name, payload)` runs them). Each module below registers its handler
when it is imported. An `order.created` payload holds `id`, `customer`,
`email`, `qty` and `total`; an `order.shipped` payload holds `id` and
`email`.

### Requirement: Audit log
`app.audit` SHALL keep a list `LOG` and append `"order <id> created"` to
it for every `order.created` event.

#### Scenario: Created
- **WHEN** `order.created` is emitted for order 7
- **THEN** `LOG` ends with `"order 7 created"`

### Requirement: Stock
`app.stock` SHALL keep a dict `STOCK` with an integer `"units"` count,
starting at 100, and subtract `qty` on every `order.created` event.

#### Scenario: Two units
- **WHEN** `order.created` is emitted with `qty` 2
- **THEN** `STOCK["units"]` falls by 2

### Requirement: Shipping mail
`app.mailer` SHALL keep a list `OUTBOX` and append
`"to <email>: order <id> shipped"` for every `order.shipped` event.

#### Scenario: Shipped
- **WHEN** `order.shipped` is emitted for order 7 to `a@x.io`
- **THEN** `OUTBOX` ends with `"to a@x.io: order 7 shipped"`

### Requirement: Loyalty points
`app.loyalty` SHALL keep a dict `POINTS` and add `total // 10` points to
the customer's entry for every `order.created` event.

#### Scenario: Points
- **WHEN** `order.created` is emitted for customer `"ada"` with total 95
- **THEN** `POINTS["ada"]` grows by 9
