from app import audit, loyalty, mailer, stock
from app.events import emit

CREATED = {"id": 41, "customer": "grace", "email": "g@x.io", "qty": 3, "total": 120}


def test_order_created_reaches_every_handler():
    units = stock.STOCK["units"]
    points = loyalty.POINTS.get("grace", 0)
    emit("order.created", dict(CREATED))
    assert audit.LOG[-1] == "order 41 created"
    assert stock.STOCK["units"] == units - 3
    assert loyalty.POINTS["grace"] == points + 12


def test_order_shipped():
    emit("order.shipped", {"id": 41, "email": "g@x.io"})
    assert mailer.OUTBOX[-1] == "to g@x.io: order 41 shipped"
