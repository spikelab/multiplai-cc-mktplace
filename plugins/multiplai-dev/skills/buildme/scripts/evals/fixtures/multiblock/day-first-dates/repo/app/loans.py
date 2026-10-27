"""The library's open loans, read from loans.json."""

import json
from pathlib import Path

_DATA = Path(__file__).with_name("loans.json")


def all_loans() -> dict:
    return json.loads(_DATA.read_text())
