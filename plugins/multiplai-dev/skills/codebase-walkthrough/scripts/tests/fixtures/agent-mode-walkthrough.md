# Module: engine/bookings

> Generated <DATE> · depth standard · mode agent · 7 files · spent $0.00 over 5 agent calls

- Module directory: `<WS>/engine/bookings`
- Depth: `standard`
- Mode: `agent`

Every `path:line` below was checked against the commit listed here. Re-check with `python -m walkthrough_pipeline check <this file>`.

## Commits read

| Repo | Path | Commit |
|---|---|---|
| engine | <WS>/engine | `8fad5fdf1b69c48c9b215ec887f8d99e3ca0ab2e` |

## Files (7)

- `bookings/__init__.py` — (no checked purpose)
- `bookings/apps.py` — part of bookings
- `bookings/client.py` — part of bookings
- `bookings/models.py` — part of bookings
- `bookings/tasks.py` — part of bookings
- `bookings/urls.py` — part of bookings
- `bookings/views.py` — part of bookings

## Architecture

This part is explained by [`engine/bookings/apps.py:1`](<WS>/engine/bookings/apps.py#L1 "from django.apps import AppConfig") and `Reservation`.

## Patterns & Conventions

This part is explained by [`engine/bookings/apps.py:1`](<WS>/engine/bookings/apps.py#L1 "from django.apps import AppConfig") and `Reservation`.

## Key Interfaces

Nothing passed the checks here.

## Gotchas

This part is explained by [`engine/bookings/models.py:5`](<WS>/engine/bookings/models.py#L5 "guest = models.CharField") and `Reservation`.

## Coverage

| Measure | Value |
|---|---|
| Files with a surviving fact | 6 of 7 (85.7%) |
| Files over 30 lines with a surviving fact | 0 of 0 (0.0%) |
| Migrations (counted, not read) | 2; latest `0002_ospite.py` |
| Boundary entries not cited by any fact | 0 |
| Facts kept | 13 |
| Claims dropped by the explore checks | 2 |
| Paragraphs cut by the write checks | 0 |

Files with no surviving fact (1):

- `engine/bookings/__init__.py` (1 lines, at or under 30: not required)
