"""Public M1 data loader, with the period lock built in.

This is the only entry point research code may use to read prices:
- any range touching OOS or HOLDOUT is refused (and journaled) unless it carries a
  granted access token (milestone 7);
- data are refused if the quality control has not been run on the current files;
- invalid, low-liquidity and partial days (quality control) are removed;
- minutes with a single side (BID without ASK or the reverse) are removed and counted.

Data before the IS start (``warmup_start``) are readable, for indicator warm-up only.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from btlab.context import DataContext
from btlab.data.quality import load_exclusions, load_summary, quality_state
from btlab.data.store import PRICE_COLS
from btlab.periods import PeriodAccess, to_utc


class QualityNotCheckedError(RuntimeError):
    """The quality control is missing or older than the data files."""


def exclusion_mask(ts: pd.DatetimeIndex, exclusions: pd.DataFrame) -> np.ndarray:
    """True for timestamps inside any ``[start_utc, end_utc)`` exclusion interval."""
    if len(ts) == 0 or exclusions.empty:
        return np.zeros(len(ts), dtype=bool)
    excl = exclusions.sort_values("start_utc")
    starts = pd.DatetimeIndex(excl["start_utc"]).as_unit("ns").asi8
    ends = pd.DatetimeIndex(excl["end_utc"]).as_unit("ns").asi8
    values = ts.as_unit("ns").asi8
    idx = np.searchsorted(starts, values, side="right") - 1
    inside = idx >= 0
    inside[inside] = values[inside] < ends[idx[inside]]
    return inside


def load_m1(
    symbol: str,
    start=None,
    end=None,
    *,
    access: PeriodAccess | None = None,
    exclude_invalid: bool = True,
    allow_unchecked: bool = False,
    ctx: DataContext | None = None,
    caller: str = "load_m1",
) -> pd.DataFrame:
    """M1 BID/ASK candles of ``[start, end)`` (UTC), indexed by ``ts_utc``.

    ``start`` defaults to the warm-up start, ``end`` to the end of the IS.
    """
    ctx = ctx or DataContext.load()
    ctx.instrument(symbol)
    pset = ctx.periods.for_symbol(symbol)
    start = to_utc(start) if start is not None else pd.Timestamp(ctx.periods.warmup_start, tz="UTC")
    end = to_utc(end) if end is not None else pset.is_end
    touched = ctx.lock.check(symbol, start, end, access=access, caller=caller)

    qroot = ctx.paths.quality
    state = quality_state(qroot, ctx.store, symbol)
    if state != "ok" and not allow_unchecked:
        what = (
            "n'a jamais été lancé" if state == "missing" else "est antérieur aux données actuelles"
        )
        raise QualityNotCheckedError(
            f"{symbol} : le contrôle qualité {what}. Lancez « btlab data qc {symbol} » "
            "(ou la page Données) avant d'utiliser ces données."
        )

    last_year = (end - pd.Timedelta(1, "ns")).year
    years = [y for y in ctx.store.years(symbol) if start.year <= y <= last_year]
    df = ctx.store.read_years(symbol, years, start, end)

    both = df[PRICE_COLS].notna().all(axis=1).to_numpy()
    n_one_sided = int((~both).sum())
    df = df[both]

    n_excluded = 0
    excluded_days = 0
    if exclude_invalid and state != "missing":
        exclusions = load_exclusions(qroot, symbol)
        exclusions = exclusions[(exclusions["end_utc"] > start) & (exclusions["start_utc"] < end)]
        mask = exclusion_mask(pd.DatetimeIndex(df["ts_utc"]), exclusions)
        n_excluded = int(mask.sum())
        excluded_days = len(exclusions)
        df = df[~mask]

    df = df.set_index("ts_utc")
    summary = load_summary(qroot, symbol) or {}
    df.attrs.update(
        symbol=symbol,
        start=start.isoformat(),
        end=end.isoformat(),
        periods=touched,
        is_start=pd.Timestamp(pset.is_.start, tz="UTC").isoformat(),
        quality_state=state,
        quality_generated_at=summary.get("generated_at"),
        dropped_one_sided=n_one_sided,
        excluded_rows=n_excluded,
        excluded_days=excluded_days,
        exclude_invalid=exclude_invalid,
    )
    return df
