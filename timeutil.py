"""Shared time helpers.

The "logical day" is Europe/Berlin local; a logical day X spans
[X 03:30 Europe/Berlin, (X+1) 03:30 Europe/Berlin).
"""

from datetime import datetime, date, time, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    BERLIN = ZoneInfo("Europe/Berlin")
except Exception:  # pragma: no cover
    BERLIN = timezone.utc


def now_berlin() -> datetime:
    return datetime.now(BERLIN)


def now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def logical_day_for(dt: datetime, boundary_hour: int, boundary_minute: int) -> str:
    """Return the logical day (YYYY-MM-DD) that `dt` belongs to.

    A logical day X starts at boundary on calendar day X. So a train at
    02:00 Berlin still belongs to the previous calendar day.
    """
    local = dt.astimezone(BERLIN)
    boundary = time(boundary_hour, boundary_minute)
    if local.time() < boundary:
        return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()


def current_logical_day(boundary_hour: int, boundary_minute: int) -> str:
    return logical_day_for(now_berlin(), boundary_hour, boundary_minute)


def previous_logical_day(boundary_hour: int, boundary_minute: int) -> str:
    today = current_logical_day(boundary_hour, boundary_minute)
    return (date.fromisoformat(today) - timedelta(days=1)).isoformat()
