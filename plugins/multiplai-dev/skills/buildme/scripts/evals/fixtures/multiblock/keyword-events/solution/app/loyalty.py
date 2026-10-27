from app.events import on

POINTS: dict[str, int] = {}


@on("order.created")
def _created(customer, total, **_):
    POINTS[customer] = POINTS.get(customer, 0) + total // 10
