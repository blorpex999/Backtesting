"""Quoting calendar: which UTC minutes an instrument is expected to quote.

Sessions are defined in the local time of the reference market. Each UTC minute is
converted to that local time with ``zoneinfo`` before testing it against the weekly
window, the daily breaks and the closed dates, so daylight saving changes (and the
weeks when Europe and the US are out of sync) are handled by construction.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache

import numpy as np
import pandas as pd

from btlab.data.instruments import SessionSpec


def _local_fields(ts_utc: pd.DatetimeIndex, tz: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Local minute-of-day, weekday (Mon=0) and MMDD (month*100+day) arrays."""
    local = ts_utc.tz_convert(tz)
    minute_of_day = local.hour.to_numpy() * 60 + local.minute.to_numpy()
    weekday = local.dayofweek.to_numpy()
    mmdd = local.month.to_numpy() * 100 + local.day.to_numpy()
    return minute_of_day, weekday, mmdd


def _in_window(values: np.ndarray, start: int, end: int) -> np.ndarray:
    """``values`` in ``[start, end)`` on a circular axis (wraps when end <= start)."""
    if start < end:
        return (values >= start) & (values < end)
    return (values >= start) | (values < end)


def open_mask(spec: SessionSpec, ts_utc: pd.DatetimeIndex) -> np.ndarray:
    """Boolean array: is the instrument expected to quote at each UTC minute start?"""
    if len(ts_utc) == 0:
        return np.zeros(0, dtype=bool)
    minute_of_day, weekday, mmdd = _local_fields(ts_utc, spec.timezone)
    minute_of_week = weekday * 1440 + minute_of_day
    mask = _in_window(
        minute_of_week, spec.weekly_open.minute_of_week, spec.weekly_close.minute_of_week
    )
    for brk in spec.daily_breaks:
        mask &= ~_in_window(minute_of_day, brk.start_minute, brk.end_minute)
    if spec.closed_dates:
        closed = np.array([int(d.replace("-", "")) for d in spec.closed_dates])
        mask &= ~np.isin(mmdd, closed)
    return mask


def minute_range(start: pd.Timestamp, end: pd.Timestamp) -> pd.DatetimeIndex:
    """All UTC minute starts in ``[start, end)``."""
    return pd.date_range(start, end, freq="min", inclusive="left", tz="UTC", unit="ns")


def expected_minutes(spec: SessionSpec, start: pd.Timestamp, end: pd.Timestamp) -> pd.DatetimeIndex:
    """UTC minute starts in ``[start, end)`` during which quotes are expected."""
    minutes = minute_range(start, end)
    return minutes[open_mask(spec, minutes)]


@lru_cache(maxsize=16)
def _calendar_sessions(code: str) -> pd.DatetimeIndex:
    import exchange_calendars as xcals

    cal = xcals.get_calendar(code, start="2000-01-03")
    return cal.sessions


def underlying_holidays(code: str | None, start: date, end: date) -> set[date]:
    """Weekdays in ``[start, end]`` on which the underlying exchange is closed."""
    if not code:
        return set()
    sessions = _calendar_sessions(code)
    days = pd.bdate_range(start, end)
    days = days[(days >= sessions[0]) & (days <= sessions[-1])]
    closed = days.difference(sessions.tz_localize(None) if sessions.tz else sessions)
    return {d.date() for d in closed}
