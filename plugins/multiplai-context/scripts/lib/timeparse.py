"""One ISO-timestamp parser, in a module with no expensive imports.

A leaf module, so a caller on the hook path can import it without pulling in
anything slow.
"""

from datetime import UTC, datetime


def parse_ts(raw) -> datetime | None:
    """ISO string -> tz-aware UTC datetime, ``None`` on anything unparseable.

    One rule for every caller — this used to exist in three copies, one of
    which had already dropped the ``Z`` normalisation.
    """
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts
