"""Quality control: every anomaly is detected on synthetic data, nothing is repaired."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from btlab.data.quality import (
    load_days,
    quality_state,
    run_quality,
    save_quality,
)
from btlab.data.synthetic import synthetic_m1


def _utc(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC")


START, END = "2019-01-06", "2019-02-02"  # Sunday -> Saturday, EURUSD


@pytest.fixture
def base(ctx) -> pd.DataFrame:
    return synthetic_m1(ctx.instrument("EURUSD"), START, END, seed=3)


def _qc(ctx, frame, symbol="EURUSD", data_end=END, issues=None, **overrides):
    ctx.store.write_frame(symbol, frame, issues)
    settings = ctx.settings.quality.model_copy(update=overrides)
    return run_quality(ctx.instrument(symbol), ctx.store, settings, data_end=_utc(data_end))


def _day(result, d: str) -> pd.Series:
    days = result.days
    return days[days["day"] == date.fromisoformat(d)].iloc[0]


def _set(frame, ts: str, **values) -> pd.DataFrame:
    frame = frame.copy()
    i = frame.index[frame["ts_utc"] == _utc(ts)][0]
    for k, v in values.items():
        frame.loc[i, k] = v
    return frame


def test_clean_data_all_valid(ctx, base):
    r = _qc(ctx, base)
    assert set(r.days["status"]) == {"valid"}, r.days[r.days["status"] != "valid"]
    assert r.anomalies.empty
    assert (r.days["coverage"].dropna() == 1.0).all()


def test_gap_warning_and_invalid(ctx, base):
    short = (base["ts_utc"] >= _utc("2019-01-08 10:00")) & (
        base["ts_utc"] < _utc("2019-01-08 10:20")
    )
    long = (base["ts_utc"] >= _utc("2019-01-09 10:00")) & (
        base["ts_utc"] < _utc("2019-01-09 11:10")
    )
    r = _qc(ctx, base[~short & ~long])
    d8, d9 = _day(r, "2019-01-08"), _day(r, "2019-01-09")
    assert d8["status"] == "valid" and d8["gaps_warn"] == 1 and d8["max_gap"] == 20
    assert d9["status"] == "invalid" and "trou de 70 min" in d9["reasons"]
    gaps = r.anomalies[r.anomalies["kind"] == "gap"]
    assert list(gaps["minutes"]) == [20, 70]
    assert gaps["start_utc"].iloc[1] == _utc("2019-01-09 10:00")


def test_low_coverage_without_long_gap(ctx, base):
    day = (base["ts_utc"] >= _utc("2019-01-10 00:00")) & (base["ts_utc"] < _utc("2019-01-10 23:00"))
    drop = day & (base["ts_utc"].dt.minute % 5 == 0)  # 20 % of minutes missing, gaps of 1 min
    r = _qc(ctx, base[~drop])
    d = _day(r, "2019-01-10")
    assert d["status"] == "invalid" and "couverture" in d["reasons"] and d["max_gap"] == 1


def test_missing_day(ctx, base):
    day = (base["ts_utc"] >= _utc("2019-01-14 23:00")) & (base["ts_utc"] < _utc("2019-01-15 23:00"))
    r = _qc(ctx, base[~day])
    d = _day(r, "2019-01-15")  # Paris day = 23:00 UTC -> 23:00 UTC
    assert d["status"] == "invalid" and "aucune donnée" in d["reasons"]


def test_ohlc_inconsistent(ctx, base):
    row = base[base["ts_utc"] == _utc("2019-01-16 12:00")].iloc[0]
    r = _qc(ctx, _set(base, "2019-01-16 12:00", bid_h=row["bid_l"] - 0.001))
    d = _day(r, "2019-01-16")
    assert d["status"] == "invalid" and d["ohlc_invalid"] == 1
    assert "incohérente" in d["reasons"]


def test_ask_below_bid(ctx, base):
    row = base[base["ts_utc"] == _utc("2019-01-17 12:00")].iloc[0]
    frame = _set(base, "2019-01-17 12:00", ask_o=row["bid_o"] - 0.0002, ask_l=row["bid_l"] - 0.0002)
    r = _qc(ctx, frame)
    d = _day(r, "2019-01-17")
    assert d["status"] == "invalid" and d["negative_spread"] == 1
    assert "ask < bid" in d["reasons"]


def test_aberrant_spread(ctx, base):
    frame = base.copy()
    mask = (frame["ts_utc"] >= _utc("2019-01-18 09:00")) & (
        frame["ts_utc"] < _utc("2019-01-18 10:00")
    )
    frame.loc[mask, "ask_c"] = frame.loc[mask, "bid_c"] + 0.0050  # ~60x the normal spread
    frame.loc[mask, "ask_h"] = np.maximum(frame.loc[mask, "ask_h"], frame.loc[mask, "ask_c"])
    r = _qc(ctx, frame)
    d = _day(r, "2019-01-18")
    assert d["spread_outliers"] == 60 and d["status"] == "invalid"
    assert "spread aberrant" in d["reasons"]
    # A few isolated wide spreads (e.g. rollover) are reported but do not invalidate the day.
    frame2 = base.copy()
    m2 = (frame2["ts_utc"] >= _utc("2019-01-22 22:00")) & (
        frame2["ts_utc"] < _utc("2019-01-22 22:03")
    )
    frame2.loc[m2, "ask_c"] = frame2.loc[m2, "bid_c"] + 0.0050
    frame2.loc[m2, "ask_h"] = np.maximum(frame2.loc[m2, "ask_h"], frame2.loc[m2, "ask_c"])
    r2 = _qc(ctx, frame2)
    d2 = _day(r2, "2019-01-22")
    assert d2["spread_outliers"] == 3 and d2["status"] == "valid"


def test_one_sided_minutes(ctx, base):
    frame = base.copy()
    few = (frame["ts_utc"] >= _utc("2019-01-21 10:00")) & (
        frame["ts_utc"] < _utc("2019-01-21 10:03")
    )
    many = (frame["ts_utc"] >= _utc("2019-01-23 10:00")) & (
        frame["ts_utc"] < _utc("2019-01-23 10:10")
    )
    frame.loc[few | many, ["ask_o", "ask_h", "ask_l", "ask_c", "ask_v"]] = np.nan
    r = _qc(ctx, frame)
    assert _day(r, "2019-01-21")["status"] == "valid"
    assert _day(r, "2019-01-21")["one_sided"] == 3
    assert _day(r, "2019-01-23")["status"] == "invalid"


def test_duplicates_from_build_are_reported(ctx, base):
    issues = pd.DataFrame(
        {
            "ts_utc": [_utc("2019-01-24 10:00"), _utc("2019-01-25 10:00")],
            "side": ["bid", "ask"],
            "kind": ["exact_duplicate", "conflicting_duplicate"],
            "detail": ["ligne identique retirée", "versions retirées"],
        }
    )
    frame = base[base["ts_utc"] != _utc("2019-01-25 10:00")]  # dropped at build time
    r = _qc(ctx, frame, issues=issues)
    assert _day(r, "2019-01-24")["status"] == "valid"
    assert _day(r, "2019-01-24")["exact_duplicates"] == 1
    d = _day(r, "2019-01-25")
    assert d["status"] == "invalid" and "doublon" in d["reasons"]
    assert set(r.anomalies["kind"]) == {"exact_duplicate", "conflicting_duplicate"}


def test_low_liquidity_days_and_holidays(ctx):
    inst = ctx.instrument("US500")
    frame = synthetic_m1(inst, "2018-12-16", "2019-01-06", seed=4, price=2600.0)
    r = _qc(ctx, frame, symbol="US500", data_end="2019-01-06")
    for d in ("2018-12-24", "2018-12-31", "2019-01-01"):
        assert _day(r, d)["status"] == "excluded_low_liquidity"
        assert "faible liquidité" in _day(r, d)["reasons"]
    r2 = _qc(ctx, frame, symbol="US500", data_end="2019-01-06", exclude_low_liquidity=False)
    assert _day(r2, "2018-12-24")["status"] == "valid"
    assert _day(r2, "2019-01-01")["status"] == "holiday"  # NYSE closed, not an error


def test_holiday_without_data_is_not_invalid(ctx):
    inst = ctx.instrument("US500")
    frame = synthetic_m1(inst, "2019-06-30", "2019-07-07", seed=5, price=2900.0)
    local = frame["ts_utc"].dt.tz_convert("Europe/Paris").dt.date
    frame = frame[local != date(2019, 7, 4)]
    r = _qc(ctx, frame, symbol="US500", data_end="2019-07-07")
    d = _day(r, "2019-07-04")
    assert d["status"] == "holiday" and d["observed"] == 0


def test_partial_last_day(ctx, base):
    r = _qc(ctx, base[base["ts_utc"] < _utc("2019-01-25")], data_end="2019-01-25")
    last = r.days.iloc[-1]
    assert str(last["day"]) == "2019-01-25" and last["status"] == "partial"
    assert r.anomalies.empty, "minutes after the end of the data are not gaps"
    assert last["max_gap"] == 0


def test_first_day_cut_by_data_start_is_partial_not_invalid(ctx):
    # Paris day 2019-01-08 starts at 2019-01-07 23:00 UTC, before the data (00:00 UTC).
    frame = synthetic_m1(ctx.instrument("EURUSD"), "2019-01-08", "2019-01-12", seed=6)
    r = _qc(ctx, frame, data_end="2019-01-12")
    first = r.days.iloc[0]
    assert str(first["day"]) == "2019-01-08" and first["status"] == "partial"
    assert "avant le début des données" in first["reasons"]
    assert r.exclusions.iloc[0]["status"] == "partial"
    # A Sunday cut before the (closed) hours has no missing expected quote: not partial.
    frame = synthetic_m1(ctx.instrument("EURUSD"), "2019-01-06", "2019-01-12", seed=6)
    r = _qc(ctx, frame, data_end="2019-01-12")
    assert r.days.iloc[0]["status"] == "valid"


def test_quotes_outside_configured_sessions_are_reported(ctx, base):
    extra = base[base["ts_utc"] == _utc("2019-01-11 21:59")].copy()  # Friday close
    extra["ts_utc"] = _utc("2019-01-12 10:00")  # Saturday
    frame = pd.concat([base, extra]).sort_values("ts_utc")
    r = _qc(ctx, frame)
    d = _day(r, "2019-01-12")
    assert d["status"] == "off_session" and d["observed_outside"] == 1
    assert "outside_session" in set(r.anomalies["kind"])


def test_nothing_is_repaired_and_state_tracking(ctx, base):
    short = (base["ts_utc"] >= _utc("2019-01-09 10:00")) & (
        base["ts_utc"] < _utc("2019-01-09 11:10")
    )
    frame = base[~short].reset_index(drop=True)
    r = _qc(ctx, frame)
    stored = ctx.store.read_years("EURUSD", [2019])
    pd.testing.assert_frame_equal(stored, frame, check_dtype=False)
    root = ctx.paths.quality
    assert quality_state(root, ctx.store, "EURUSD") == "missing"
    save_quality(r, root)
    assert quality_state(root, ctx.store, "EURUSD") == "ok"
    assert len(load_days(root, "EURUSD")) == len(r.days)
    ctx.store.write_frame("EURUSD", base)  # data changed -> QC stale
    assert quality_state(root, ctx.store, "EURUSD") == "stale"


def test_exclusions_are_utc_intervals_of_local_days(ctx, base):
    assert _qc(ctx, base).exclusions.empty
    day = (base["ts_utc"] >= _utc("2019-01-14 23:00")) & (base["ts_utc"] < _utc("2019-01-15 23:00"))
    r = _qc(ctx, base[~day])
    row = r.exclusions.iloc[0]
    assert row["start_utc"] == _utc("2019-01-14 23:00") and row["end_utc"] == _utc(
        "2019-01-15 23:00"
    )
