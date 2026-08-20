"""One ISO-timestamp parser, in a module with no expensive imports.

It lives here rather than in ``lib.fleet_sources.common`` because of who needs
it. ``lib.fleet`` runs from session hooks and must stay a pure file read; the
collectors in ``lib.fleet_sources`` shell out to ``git`` and ``gh``. Importing
one name out of that package pulls its ``__init__`` and therefore all four
collectors — measured at ~16-22 ms and ``subprocess`` resident, on a module the
hook path imports every session, for a six-line function.

A leaf module keeps the single definition the collectors and the fast path
share, without the fast path paying for the collectors.
"""

from datetime import datetime, timezone


def parse_ts(raw) -> datetime | None:
    """ISO string -> tz-aware UTC datetime, ``None`` on anything unparseable.

    One rule for every fleet source — this used to exist in three copies, one
    of which had already dropped the ``Z`` normalisation.
    """
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
