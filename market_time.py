"""Shared market-clock helpers; independent of the machine's local timezone."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo


LOS_ANGELES_TZ = ZoneInfo("America/Los_Angeles")
NEW_YORK_TZ = ZoneInfo("America/New_York")


def los_angeles_now() -> dt.datetime:
    """Return the current timezone-aware time in Los Angeles."""
    return dt.datetime.now(LOS_ANGELES_TZ)


def los_angeles_today() -> dt.date:
    """Return today's calendar date in Los Angeles."""
    return los_angeles_now().date()


def to_los_angeles_time(value: object, *, naive_timezone: dt.tzinfo = dt.timezone.utc) -> dt.datetime | None:
    """Parse an ISO timestamp and convert it to Los Angeles time.

    Cboe top-level timestamps are UTC but omit the offset. Callers can supply a
    different source timezone for other naive timestamps.
    """
    if isinstance(value, dt.datetime):
        timestamp = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            timestamp = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=naive_timezone)
    return timestamp.astimezone(LOS_ANGELES_TZ)


def format_cboe_timestamp(value: object) -> str:
    """Format a naive-UTC Cboe response timestamp in Los Angeles time."""
    timestamp = to_los_angeles_time(value)
    return timestamp.strftime("%Y-%m-%d %H:%M:%S %Z") if timestamp is not None else str(value or "")
