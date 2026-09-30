"""Quoting calendar across daylight saving changes (EU and US switch on different weekends)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from btlab.data.sessions import expected_minutes, underlying_holidays


def _utc(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC")


def _first(ctx, symbol: str, start: str, end: str) -> pd.Timestamp:
    return expected_minutes(ctx.instrument(symbol).sessions, _utc(start), _utc(end))[0]


@pytest.mark.parametrize(
    ("sunday", "open_utc", "case"),
    [
        ("2023-01-08", "2023-01-08 22:00", "hiver : 17:00 New York = 22:00 UTC"),
        ("2023-03-12", "2023-03-12 21:00", "jour du passage à l'heure d'été US"),
        ("2023-03-19", "2023-03-19 21:00", "semaine décalée : US en été, Europe en hiver"),
        ("2023-03-26", "2023-03-26 21:00", "jour du passage à l'heure d'été européenne"),
        ("2023-07-09", "2023-07-09 21:00", "été"),
        (
            "2023-10-29",
            "2023-10-29 21:00",
            "semaine décalée d'automne (Europe en hiver, US en été)",
        ),
        ("2023-11-05", "2023-11-05 22:00", "retour à l'heure d'hiver US"),
    ],
)
def test_forex_weekly_open_follows_new_york_time(ctx, sunday, open_utc, case):
    first = _first(ctx, "EURUSD", sunday, pd.Timestamp(sunday) + pd.Timedelta(days=1))
    assert first == _utc(open_utc), case


def test_forex_friday_close_during_autumn_shift_week(ctx):
    spec = ctx.instrument("EURUSD").sessions
    # 2023-11-03: Paris already in winter time (CET), New York still in summer time (EDT).
    minutes = expected_minutes(spec, _utc("2023-11-03"), _utc("2023-11-04"))
    assert minutes[-1] == _utc("2023-11-03 20:59")  # close 17:00 EDT = 21:00 UTC = 22:00 Paris
    assert minutes[-1].tz_convert("Europe/Paris").hour == 21


@pytest.mark.parametrize(
    ("monday", "open_utc"),
    [
        ("2023-03-20", "2023-03-20 00:15"),  # CET: 01:15 Berlin = 00:15 UTC
        ("2023-03-27", "2023-03-26 23:15"),
    ],  # CEST: 01:15 Berlin = 23:15 UTC the day before
)
def test_ger40_open_follows_berlin_time(ctx, monday, open_utc):
    start = pd.Timestamp(monday) - pd.Timedelta(days=1)
    assert _first(ctx, "GER40", start, monday + " 12:00") == _utc(open_utc)


def test_break_wrapping_midnight(ctx):
    spec = ctx.instrument("GER40").sessions  # daily break 22:00 -> 01:15 Berlin
    minutes = expected_minutes(spec, _utc("2023-03-28"), _utc("2023-03-29"))
    local = minutes.tz_convert("Europe/Berlin")
    assert not (
        (local.hour >= 22) | (local.hour == 0) | ((local.hour == 1) & (local.minute < 15))
    ).any()
    assert local.min().strftime("%H:%M") == "02:00"  # 00:00 UTC = 02:00 CEST, still open
    assert _utc("2023-03-28 19:59") in minutes and _utc("2023-03-28 20:00") not in minutes


@pytest.mark.parametrize("day", ["2023-01-10", "2023-03-14", "2023-07-11", "2023-10-31"])
def test_us_index_daily_break_is_one_hour_whatever_the_season(ctx, day):
    spec = ctx.instrument("US100").sessions  # break 17:00-18:00 New York
    minutes = expected_minutes(spec, _utc(day), _utc(day) + pd.Timedelta(days=1))
    assert len(minutes) == 23 * 60
    gap = minutes.to_series().diff().max()
    assert gap == pd.Timedelta(minutes=61)


def test_closed_dates(ctx):
    spec = ctx.instrument("EURUSD").sessions  # 12-25 closed (New York date)
    minutes = expected_minutes(spec, _utc("2023-12-25 05:00"), _utc("2023-12-26 05:00"))
    assert len(minutes) == 0


def test_underlying_holidays():
    us = underlying_holidays("XNYS", date(2019, 1, 1), date(2019, 12, 31))
    de = underlying_holidays("XETR", date(2019, 1, 1), date(2019, 12, 31))
    assert date(2019, 7, 4) in us and date(2019, 11, 28) in us
    assert date(2019, 12, 24) in de and date(2019, 7, 4) not in de
    assert underlying_holidays(None, date(2019, 1, 1), date(2019, 12, 31)) == set()
