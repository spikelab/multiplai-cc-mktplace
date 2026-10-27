from app.events import on

STOCK = {"units": 100}


@on("order.created")
def _created(qty, **_):
    STOCK["units"] -= qty
