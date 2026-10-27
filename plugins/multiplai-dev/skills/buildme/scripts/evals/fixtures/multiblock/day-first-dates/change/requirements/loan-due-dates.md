## ADDED Requirements

Loans come from `app.loans.all_loans()`, keyed by loan id. Today's date
comes from `app.clock.today()`; tests fix it with
`app.clock.set_today(date)`. The shipped loans are 1 (Dune, due
3 February 2026), 2 (Emma, due 11 February 2026) and 3 (Ulysses, due
5 March 2026).

### Requirement: Overdue
`app.due.is_overdue(loan_id)` SHALL be true when today is after the due
date.

#### Scenario: Overdue
- **WHEN** today is 4 February 2026
- **THEN** `is_overdue("1")` is true and `is_overdue("2")` is false

### Requirement: Days left
`app.due.days_left(loan_id)` SHALL return the number of days from today
to the due date (negative when overdue).

#### Scenario: Days left
- **WHEN** today is 1 February 2026
- **THEN** `days_left("2")` is 10

### Requirement: Overdue list
`app.overdue.overdue_ids()` SHALL return the ids of overdue loans,
sorted.

#### Scenario: List
- **WHEN** today is 12 February 2026
- **THEN** `overdue_ids()` returns `["1", "2"]`

### Requirement: Reminder
`app.reminders.reminder(loan_id)` SHALL return
`"Please return <title> by <YYYY-MM-DD>"`.

#### Scenario: Reminder
- **WHEN** `reminder("3")` is called
- **THEN** it returns `"Please return Ulysses by 2026-03-05"`
