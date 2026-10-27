"""Read-only access to the shop's records, loaded from data.json."""

import json
from pathlib import Path

_DATA = Path(__file__).with_name("data.json")


class Store:
    def __init__(self, path=_DATA):
        raw = json.loads(Path(path).read_text())
        self._users = raw["users"]
        self._orders = raw["orders"]

    def user(self, user_id):
        return self._users[user_id]

    def order(self, order_id):
        return self._orders[order_id]
