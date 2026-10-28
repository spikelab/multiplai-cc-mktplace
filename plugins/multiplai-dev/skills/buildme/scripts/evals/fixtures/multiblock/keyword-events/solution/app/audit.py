from app.events import on

LOG: list[str] = []


@on("order.created")
def _created(id, **_):
    LOG.append(f"order {id} created")
