from app.events import on

OUTBOX: list[str] = []


@on("order.shipped")
def _shipped(id, email, **_):
    OUTBOX.append(f"to {email}: order {id} shipped")
