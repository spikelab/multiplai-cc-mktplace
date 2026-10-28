from datetime import date

from app.clock import set_today
from app.due import days_left, is_overdue
from app.overdue import overdue_ids
from app.reminders import reminder


def test_due_dates_are_day_first():
    set_today(date(2026, 2, 10))
    assert is_overdue("1") is True
    assert is_overdue("2") is False
    assert days_left("3") == 23
    assert overdue_ids() == ["1"]
    set_today(date(2026, 3, 6))
    assert overdue_ids() == ["1", "2", "3"]
    set_today(None)


def test_reminders():
    assert reminder("1") == "Please return Dune by 2026-02-03"
    assert reminder("2") == "Please return Emma by 2026-02-11"
