# Tasks

## 1. Overdue check

Add `app/due.py` with `is_overdue`.

Interfaces:
- Produces: app.due.is_overdue(loan_id: str) -> bool

Satisfies: Overdue

## 2. Days left

Add `days_left` to `app/due.py`.

Interfaces:
- Produces: app.due.days_left(loan_id: str) -> int

Satisfies: Days left

## 3. Overdue list

Add `app/overdue.py` with `overdue_ids`.

Interfaces:
- Produces: app.overdue.overdue_ids() -> list[str]

Satisfies: Overdue list

## 4. Reminder

Add `app/reminders.py` with `reminder`, formatting the due date as
`YYYY-MM-DD`.

Interfaces:
- Produces: app.reminders.reminder(loan_id: str) -> str

Satisfies: Reminder
