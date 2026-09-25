"""Shared market-clock helpers; independent of the machine's local timezone."""

from __future__ import annotations

import datetime as dt
from collections.abc import Collection
from zoneinfo import ZoneInfo


LOS_ANGELES_TZ = ZoneInfo("America/Los_Angeles")
NEW_YORK_TZ = ZoneInfo("America/New_York")

# Regular session opens 09:30 ET (06:30 PT). VIX/SPX options and the VIX index
# stop at 16:15 ET (13:15 PT), and Cboe's free quotes are 15 minutes delayed,
# so a session's data is final from 13:30 PT.
SESSION_OPEN_LA = dt.time(6, 30)
SESSION_DATA_FINAL_LA = dt.time(13, 30)


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


def _is_trading_day(d: dt.date, holidays: Collection[dt.date]) -> bool:
    return d.weekday() < 5 and d not in holidays


def last_completed_session(now: dt.datetime | None = None, holidays: Collection[dt.date] = ()) -> dt.date:
    """Latest trading day whose data is final as of `now` on the Los Angeles clock.

    Before 13:30 PT on a trading day (pre-market or while trading) this is the
    previous trading day.
    """
    now = los_angeles_now() if now is None else to_los_angeles_time(now)
    d = now.date()
    if _is_trading_day(d, holidays) and now.time() >= SESSION_DATA_FINAL_LA:
        return d
    d -= dt.timedelta(days=1)
    while not _is_trading_day(d, holidays):
        d -= dt.timedelta(days=1)
    return d


def snapshot_trade_date(generated_at: dt.datetime, holidays: Collection[dt.date] = ()) -> tuple[dt.date, bool]:
    """(trading day, complete) for a Cboe response generated at `generated_at`.

    While a trading day's session is still running the data is that day's,
    but incomplete; otherwise it is the latest completed session's final data.
    """
    ts = to_los_angeles_time(generated_at)
    if _is_trading_day(ts.date(), holidays) and SESSION_OPEN_LA <= ts.time() < SESSION_DATA_FINAL_LA:
        return ts.date(), False
    return last_completed_session(ts, holidays), True


def snapshot_session_date(generated_at: dt.datetime, holidays: Collection[dt.date] = ()) -> dt.date | None:
    """Trading session whose final data a Cboe response generated at `generated_at` holds.

    None when it was generated while that session was still trading (partial
    data). Before the open, or on a weekend/holiday, Cboe still serves the
    previous session's close.
    """
    ts = to_los_angeles_time(generated_at)
    if _is_trading_day(ts.date(), holidays) and SESSION_OPEN_LA <= ts.time() < SESSION_DATA_FINAL_LA:
        return None
    return last_completed_session(ts, holidays)
