"""Synthetic M1 BID/ASK data following an instrument's quoting calendar (tests, demos)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from btlab.data.instruments import Instrument
from btlab.data.sessions import expected_minutes
from btlab.data.store import COLUMNS


def synthetic_m1(
    inst: Instrument,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    seed: int = 0,
    price: float = 1.1,
    step: float | None = None,
    spread: float | None = None,
) -> pd.DataFrame:
    """Random-walk candles on every expected minute of ``[start, end)`` (UTC)."""
    start = (
        pd.Timestamp(start, tz="UTC") if pd.Timestamp(start).tzinfo is None else pd.Timestamp(start)
    )
    end = pd.Timestamp(end, tz="UTC") if pd.Timestamp(end).tzinfo is None else pd.Timestamp(end)
    ts = expected_minutes(inst.sessions, start, end)
    n = len(ts)
    step = inst.pip_size * 0.5 if step is None else step
    spread = inst.pip_size * 0.8 if spread is None else spread
    rng = np.random.default_rng(seed)
    close = price + np.cumsum(rng.normal(0.0, step, n))
    open_ = np.concatenate([[price], close[:-1]])
    wick_hi = np.abs(rng.normal(0.0, step / 2, n))
    wick_lo = np.abs(rng.normal(0.0, step / 2, n))
    high = np.maximum(open_, close) + wick_hi
    low = np.minimum(open_, close) - wick_lo
    dec = inst.price_decimals
    half = spread / 2
    data = {"ts_utc": ts}
    for side, sign in (("bid", -1.0), ("ask", 1.0)):
        for key, arr in (("o", open_), ("h", high), ("l", low), ("c", close)):
            data[f"{side}_{key}"] = np.round(arr + sign * half, dec)
    data["bid_v"] = np.round(rng.uniform(0.5, 5.0, n), 2)
    data["ask_v"] = np.round(rng.uniform(0.5, 5.0, n), 2)
    return pd.DataFrame(data, columns=COLUMNS)
