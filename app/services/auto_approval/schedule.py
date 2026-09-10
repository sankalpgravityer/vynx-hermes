"""Schedule-window arithmetic. THE AUTHORITY.

vnyx-api carries a mirror of this in TypeScript so the config screen can answer
`windowOpenNow` without a round trip, but this is the copy that decides whether
work happens. The two must agree; there is a shared fixture table in
tests/test_aa_schedule.py.

Pure: no database, no clock beyond the `now` passed in.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class Window:
    schedule_enabled: bool
    start_minute: int
    end_minute: int
    timezone: str
    days: tuple[int, ...]        # ISO-8601: 1 = Monday .. 7 = Sunday


def _zone(name: str) -> ZoneInfo:
    """Resolve an IANA zone, with a diagnosable failure.

    On Windows there is no system tz database, so this raises unless `tzdata` is
    installed — which is why requirements.txt pins it for win32. Left to
    propagate as-is the message is just the zone name, so it is wrapped.
    """
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ZoneInfoNotFoundError(
            f"Unknown time zone {name!r}. On Windows this usually means the "
            f"`tzdata` package is missing from the worker's environment."
        ) from exc


def in_window(w: Window, now: datetime) -> bool:
    """Is `now` inside the window?

    Local wall-clock arithmetic via zoneinfo, NOT a stored UTC offset. A fixed
    offset is wrong twice a year: Europe/Amsterdam is +01:00 in January and
    +02:00 in July, so an 18:00 window computed from a stored offset drifts by
    an hour. zoneinfo resolves the zone's rules for the actual instant — the
    same thing BullMQ's `tz` option does for the review-stats digest on the
    other side.
    """
    if not w.schedule_enabled or not w.days:
        return False

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(_zone(w.timezone))
    minutes = local.hour * 60 + local.minute
    iso_day = local.isoweekday()

    start, end = w.start_minute, w.end_minute

    # start == end means a 24h window, not a zero-length one: someone setting
    # 00:00 -> 00:00 means "always", and reading it as zero-length would make
    # the agent silently never run.
    if start == end:
        return iso_day in w.days

    if start < end:
        # Same-day window: 09:00 -> 17:00.
        return iso_day in w.days and start <= minutes < end

    # OVERNIGHT -- 18:00 -> 06:00. The window WRAPS, and the day filter matches
    # the day it OPENED, not the day it is now. A Friday-only 18:00->06:00
    # window must still be open at 02:00 on Saturday; testing today's weekday
    # would close it at midnight, which is not what "Friday night" means to
    # anyone.
    if minutes >= start:
        return iso_day in w.days                       # evening side
    prev = 7 if iso_day == 1 else iso_day - 1
    return prev in w.days                              # small hours


def next_window_opens_at(w: Window, now: datetime) -> datetime | None:
    """The next instant the window opens, or None when the schedule is off.

    Walks forward a minute at a time for up to 8 days — 11,520 iterations of
    pure arithmetic, microseconds, and correct across DST transitions and day
    filters without special-casing either. An analytic solution would have to
    reimplement the tz database's transition rules to reach the same answer.
    """
    if not w.schedule_enabled or not w.days:
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    at = now.replace(second=0, microsecond=0)
    was_open = in_window(w, at)
    for _ in range(8 * 24 * 60):
        at += timedelta(minutes=1)
        open_now = in_window(w, at)
        if open_now and not was_open:
            return at
        was_open = open_now
    return None


def hhmm(minute_of_day: int) -> str:
    """1080 -> '18:00'. For log lines."""
    return f"{(minute_of_day // 60) % 24:02d}:{minute_of_day % 60:02d}"


def window_from_row(row: dict) -> Window:
    """Build a Window from an AutoApprovalConfig row."""
    days = row.get("scheduleDays") or []
    return Window(
        schedule_enabled=bool(row.get("scheduleEnabled")),
        start_minute=int(row.get("scheduleStartMinute") or 0),
        end_minute=int(row.get("scheduleEndMinute") or 0),
        timezone=row.get("scheduleTimezone") or "UTC",
        days=tuple(int(d) for d in days),
    )
