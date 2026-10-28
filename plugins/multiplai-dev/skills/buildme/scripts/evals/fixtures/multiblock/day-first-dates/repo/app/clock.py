"""Today's date, which tests can fix with set_today."""

from datetime import date

_fixed: date | None = None


def set_today(value: date | None) -> None:
    global _fixed
    _fixed = value


def today() -> date:
    return _fixed or date.today()
