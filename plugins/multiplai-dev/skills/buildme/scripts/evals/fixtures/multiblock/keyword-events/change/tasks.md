# Tasks

## 1. Audit log

Add `app/audit.py` with `LOG` and an `order.created` handler.

Interfaces:
- Produces: app.audit.LOG: list[str]

Satisfies: Audit log

## 2. Stock

Add `app/stock.py` with `STOCK` and an `order.created` handler.

Interfaces:
- Produces: app.stock.STOCK: dict[str, int]

Satisfies: Stock

## 3. Shipping mail

Add `app/mailer.py` with `OUTBOX` and an `order.shipped` handler.

Interfaces:
- Produces: app.mailer.OUTBOX: list[str]

Satisfies: Shipping mail

## 4. Loyalty points

Add `app/loyalty.py` with `POINTS` and an `order.created` handler.

Interfaces:
- Produces: app.loyalty.POINTS: dict[str, int]

Satisfies: Loyalty points
