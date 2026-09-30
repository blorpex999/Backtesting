"""Loader and period lock: IS only, refusals journaled, forged tokens refused."""

from __future__ import annotations

import pandas as pd
import pyarrow.parquet as pq
import pytest

from btlab.data.loader import QualityNotCheckedError, exclusion_mask, load_m1
from btlab.data.quality import run_quality, save_quality
from btlab.data.synthetic import synthetic_m1
from btlab.periods import PeriodAccess, PeriodLockedError


def _utc(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC")


@pytest.fixture
def loaded(ctx):
    """EURUSD synthetic data around the IS/OOS boundary, with the QC run."""
    inst = ctx.instrument("EURUSD")
    frame = synthetic_m1(inst, "2019-12-01", "2020-01-20", seed=7)
    gap = (frame["ts_utc"] >= _utc("2019-12-10 10:00")) & (
        frame["ts_utc"] < _utc("2019-12-10 12:00")
    )
    frame = frame[~gap].copy()
    one_side = frame["ts_utc"] == _utc("2019-12-11 10:00")
    frame.loc[one_side, ["bid_o", "bid_h", "bid_l", "bid_c"]] = float("nan")
    ctx.store.write_frame("EURUSD", frame)
    save_quality(
        run_quality(inst, ctx.store, ctx.settings.quality, data_end=_utc("2020-01-20")),
        ctx.paths.quality,
    )
    return frame


def test_is_range_loads(ctx, loaded):
    df = load_m1("EURUSD", "2019-12-02", "2019-12-07", ctx=ctx)
    assert df.index.min() >= _utc("2019-12-02") and df.index.max() < _utc("2019-12-07")
    assert str(df.index.dtype) == "datetime64[ns, UTC]"
    assert df.attrs["periods"] == ["is"] and df.attrs["quality_state"] == "ok"


def test_last_is_minute_allowed_first_oos_minute_refused(ctx, loaded):
    df = load_m1("EURUSD", "2019-12-31", "2020-01-01", ctx=ctx, exclude_invalid=False)
    assert df.index.max() == _utc("2019-12-31 23:59")  # last IS minute is readable
    df = load_m1("EURUSD", "2019-12-31 23:00", "2020-01-01 00:00", ctx=ctx)  # end exclusive
    assert df.empty
    with pytest.raises(PeriodLockedError, match="OOS de EURUSD"):
        load_m1("EURUSD", "2019-12-31", "2020-01-01 00:01", ctx=ctx)


@pytest.mark.parametrize(
    ("start", "end", "label"),
    [
        ("2020-01-02", "2020-01-10", "OOS"),
        ("2019-06-01", "2021-01-01", "OOS"),
        ("2023-06-01", "2024-06-01", "OOS et HOLDOUT"),
        ("2025-01-01", "2025-02-01", "HOLDOUT"),
    ],
)
def test_protected_periods_refused(ctx, loaded, start, end, label):
    with pytest.raises(PeriodLockedError, match=f"touche la période {label} "):
        load_m1("EURUSD", start, end, ctx=ctx)


def test_default_end_is_the_end_of_is(ctx, loaded):
    df = load_m1("EURUSD", ctx=ctx)
    assert df.index.max() < _utc("2020-01-01")
    assert df.attrs["end"] == _utc("2020-01-01").isoformat()


def test_refusals_are_journaled(ctx, loaded):
    with pytest.raises(PeriodLockedError):
        load_m1("EURUSD", "2020-01-02", "2020-01-10", ctx=ctx, caller="test")
    log = ctx.registry.period_access_log()
    assert log[0]["status"] == "refused" and log[0]["period"] == "oos"
    assert log[0]["symbol"] == "EURUSD" and log[0]["caller"] == "test"
    assert log[0]["reason"] == "aucun jeton d'accès"


def test_forged_token_refused(ctx, loaded):
    forged = PeriodAccess(token="deadbeef", symbol="EURUSD", period="oos")
    with pytest.raises(PeriodLockedError, match="jeton inconnu du registre"):
        load_m1("EURUSD", "2020-01-02", "2020-01-10", ctx=ctx, access=forged)
    assert ctx.registry.period_access_log()[0]["token"] == "deadbeef"


def test_granted_token_is_limited_to_its_symbol_and_period(ctx, loaded):
    # Granting arrives with milestone 7; here we only check that the loader verifies grants.
    token = ctx.registry.record_grant(
        symbol="EURUSD", period="oos", reason="test", strategy_hash="s", family_hash="f"
    )
    access = PeriodAccess(token=token, symbol="EURUSD", period="oos")
    df = load_m1("EURUSD", "2020-01-02", "2020-01-10", ctx=ctx, access=access)
    assert df.attrs["periods"] == ["oos"] and not df.empty
    with pytest.raises(PeriodLockedError, match="limité à la période OOS"):
        load_m1("EURUSD", "2023-12-01", "2024-02-01", ctx=ctx, access=access)
    with pytest.raises(PeriodLockedError, match="autre instrument"):
        load_m1(
            "GBPUSD",
            "2020-01-02",
            "2020-01-10",
            ctx=ctx,
            access=PeriodAccess(token=token, symbol="GBPUSD", period="oos"),
        )


def test_request_access_always_refused_before_milestone_7(ctx):
    with pytest.raises(PeriodLockedError, match="jalon 7"):
        ctx.lock.request_access("EURUSD", "oos")
    assert ctx.registry.period_access_log()[0]["status"] == "refused"


def test_warmup_before_is_start_allowed(ctx):
    inst = ctx.instrument("EURUSD")
    frame = synthetic_m1(inst, "2009-06-01", "2009-06-06", seed=8)
    ctx.store.write_frame("EURUSD", frame)
    save_quality(
        run_quality(inst, ctx.store, ctx.settings.quality, data_end=_utc("2009-06-06")),
        ctx.paths.quality,
    )
    df = load_m1("EURUSD", "2009-06-01", "2009-06-06", ctx=ctx)
    assert len(df) > 0 and df.attrs["periods"] == []
    assert df.attrs["is_start"] == _utc("2010-01-01").isoformat()


def test_oos_files_are_never_read(ctx, loaded, monkeypatch):
    opened = []
    real = pq.read_table

    def spy(path, *args, **kwargs):
        opened.append(str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(pq, "read_table", spy)
    load_m1("EURUSD", "2019-12-01", "2020-01-01", ctx=ctx)
    assert opened and not any("2020.parquet" in p for p in opened)
    opened.clear()
    with pytest.raises(PeriodLockedError):
        load_m1("EURUSD", "2019-12-01", "2020-02-01", ctx=ctx)
    assert opened == []


def test_quality_control_required(ctx):
    inst = ctx.instrument("EURUSD")
    ctx.store.write_frame("EURUSD", synthetic_m1(inst, "2019-03-03", "2019-03-09", seed=9))
    with pytest.raises(QualityNotCheckedError, match="jamais été lancé"):
        load_m1("EURUSD", "2019-03-03", "2019-03-09", ctx=ctx)
    df = load_m1("EURUSD", "2019-03-03", "2019-03-09", ctx=ctx, allow_unchecked=True)
    assert df.attrs["quality_state"] == "missing"
    save_quality(
        run_quality(inst, ctx.store, ctx.settings.quality, data_end=_utc("2019-03-09")),
        ctx.paths.quality,
    )
    ctx.store.write_frame("EURUSD", synthetic_m1(inst, "2019-03-03", "2019-03-09", seed=10))
    with pytest.raises(QualityNotCheckedError, match="antérieur aux données"):
        load_m1("EURUSD", "2019-03-03", "2019-03-09", ctx=ctx)


def test_invalid_days_and_one_sided_minutes_removed(ctx, loaded):
    df = load_m1("EURUSD", "2019-12-09", "2019-12-13", ctx=ctx)
    local_days = set(df.index.tz_convert("Europe/Paris").date.astype(str))
    assert "2019-12-10" not in local_days  # 2 h gap -> invalid day, excluded
    assert "2019-12-11" in local_days
    assert _utc("2019-12-11 10:00") not in df.index  # BID missing -> removed
    assert df.attrs["dropped_one_sided"] == 1 and df.attrs["excluded_days"] == 1
    raw = load_m1("EURUSD", "2019-12-09", "2019-12-13", ctx=ctx, exclude_invalid=False)
    assert "2019-12-10" in set(raw.index.tz_convert("Europe/Paris").date.astype(str))


def test_exclusion_mask_edges():
    ts = pd.DatetimeIndex(
        [
            _utc("2020-01-01 22:59"),
            _utc("2020-01-01 23:00"),
            _utc("2020-01-02 22:59"),
            _utc("2020-01-02 23:00"),
        ]
    )
    excl = pd.DataFrame(
        {"start_utc": [_utc("2020-01-01 23:00")], "end_utc": [_utc("2020-01-02 23:00")]}
    )
    assert list(exclusion_mask(ts, excl)) == [False, True, True, False]


def test_unknown_symbol(ctx):
    with pytest.raises(KeyError, match="Instrument inconnu"):
        load_m1("BTCUSD", ctx=ctx)
