"""Data quality control.

Nothing is repaired here. Every anomaly is listed; a day that fails a check is marked
invalid with its reasons, and invalid, low-liquidity and partial days (cut by the start
or the end of the data) become exclusion intervals in UTC that the loader applies.
The only data change happens earlier, at build time: exact duplicate rows are removed
and conflicting duplicates dropped, and both are recorded as build issues that this
module reports.

"Days" are calendar days in ``quality.day_timezone`` (Europe/Paris by default), stored
as ``[start_utc, end_utc)`` intervals so any strategy timezone can use them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from btlab import __version__
from btlab.data.instruments import Instrument
from btlab.data.sessions import expected_minutes, open_mask, underlying_holidays
from btlab.data.settings import QualitySettings
from btlab.data.store import PriceStore

MINUTE = pd.Timedelta(minutes=1)

STATUS_LABELS = {
    "valid": "valide",
    "invalid": "invalide",
    "excluded_low_liquidity": "exclu (faible liquidité)",
    "holiday": "férié (sous-jacent)",
    "off_session": "hors séance configurée",
    "partial": "incomplet (début ou fin des données)",
}
# Partial days (cut by the start or the end of the downloaded data) are excluded too:
# an incomplete day must not be traded as if it were complete.
EXCLUDED_STATUSES = ("invalid", "excluded_low_liquidity", "partial")

ANOMALY_LABELS = {
    "gap": "trou de cotation",
    "one_sided": "BID ou ASK manquant",
    "ohlc_invalid": "bougie incohérente (OHLC)",
    "negative_spread": "ask < bid",
    "spread_outlier": "spread aberrant",
    "outside_session": "cotation hors séance configurée",
    "exact_duplicate": "doublon identique (retiré à la construction)",
    "conflicting_duplicate": "doublon contradictoire (retiré à la construction)",
    "outside_requested_range": "lignes hors plage demandée (ignorées)",
    "source_missing": "jour refusé par la source (nouvel essai au prochain téléchargement)",
    "source_unavailable": "jour indisponible chez la source (abandonné)",
}
WEEKDAY_LABELS = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]

DAY_COLUMNS = [
    "day",
    "start_utc",
    "end_utc",
    "status",
    "reasons",
    "expected",
    "observed",
    "observed_outside",
    "coverage",
    "max_gap",
    "gaps_warn",
    "one_sided",
    "ohlc_invalid",
    "negative_spread",
    "spread_outliers",
    "median_spread",
    "exact_duplicates",
    "conflicting_duplicates",
    "source_refused",
    "holiday",
    "low_liquidity",
]
ANOMALY_COLUMNS = ["start_utc", "end_utc", "day", "kind", "minutes", "detail"]


class QualityError(RuntimeError):
    pass


@dataclass
class QualityResult:
    symbol: str
    days: pd.DataFrame
    anomalies: pd.DataFrame
    heatmap: pd.DataFrame
    summary: dict

    @property
    def exclusions(self) -> pd.DataFrame:
        return exclusions_from_days(self.days)


# --- helpers ------------------------------------------------------------------------
def _local_day(ts: pd.DatetimeIndex, tz: str) -> np.ndarray:
    return ts.tz_convert(tz).tz_localize(None).to_numpy().astype("datetime64[D]")


def _day_bounds(days: np.ndarray, tz: str) -> tuple[pd.DatetimeIndex, pd.DatetimeIndex]:
    start = pd.DatetimeIndex(days.astype("datetime64[ns]")).tz_localize(tz).tz_convert("UTC")
    end = pd.DatetimeIndex((days + 1).astype("datetime64[ns]")).tz_localize(tz).tz_convert("UTC")
    return start, end


def _runs(ts: pd.DatetimeIndex) -> pd.DataFrame:
    """Group sorted minute timestamps into runs of consecutive minutes."""
    if len(ts) == 0:
        return pd.DataFrame(
            {
                "start": pd.DatetimeIndex([], tz="UTC"),
                "end": pd.DatetimeIndex([], tz="UTC"),
                "minutes": np.zeros(0, dtype=int),
            }
        )
    v = ts.asi8
    brk = np.flatnonzero(np.diff(v) != MINUTE.value)
    starts = np.r_[0, brk + 1]
    ends = np.r_[brk, len(v) - 1]
    return pd.DataFrame(
        {"start": ts[starts], "end": ts[ends] + MINUTE, "minutes": ends - starts + 1}
    )


def _count_by_day(flag: np.ndarray, day: np.ndarray) -> pd.Series:
    if not len(day):
        return pd.Series(dtype="int64")
    return pd.Series(flag.astype("int64"), index=day).groupby(level=0).sum()


def _anomaly_rows(runs: pd.DataFrame, kind: str, tz: str, detail: str = "") -> pd.DataFrame:
    if runs.empty:
        return pd.DataFrame(columns=ANOMALY_COLUMNS)
    return pd.DataFrame(
        {
            "start_utc": runs["start"].to_numpy(),
            "end_utc": runs["end"].to_numpy(),
            "day": _local_day(pd.DatetimeIndex(runs["start"]), tz),
            "kind": kind,
            "minutes": runs["minutes"].to_numpy(),
            "detail": detail,
        },
        columns=ANOMALY_COLUMNS,
    )


# --- per-window analysis ----------------------------------------------------------
@dataclass
class _WindowStats:
    days: pd.DataFrame
    anomalies: list[pd.DataFrame]
    observed_heat: np.ndarray
    expected_heat: np.ndarray


def _heat(ts: pd.DatetimeIndex, tz: str) -> np.ndarray:
    grid = np.zeros((7, 24))
    if len(ts):
        local = ts.tz_convert(tz)
        np.add.at(grid, (local.dayofweek.to_numpy(), local.hour.to_numpy()), 1)
    return grid


def _analyse_window(
    inst: Instrument,
    qs: QualitySettings,
    df: pd.DataFrame,
    issues: pd.DataFrame,
    ws: pd.Timestamp,
    we: pd.Timestamp,
) -> _WindowStats:
    tz = qs.day_timezone
    spec = inst.sessions
    eps = 0.5 * 10 ** (-inst.price_decimals)
    ts = pd.DatetimeIndex(df["ts_utc"])

    bid_ok = df[["bid_o", "bid_h", "bid_l", "bid_c"]].notna().all(axis=1).to_numpy()
    ask_ok = df[["ask_o", "ask_h", "ask_l", "ask_c"]].notna().all(axis=1).to_numpy()
    both = bid_ok & ask_ok
    one_sided = bid_ok ^ ask_ok
    in_session = open_mask(spec, ts)

    ohlc_bad = np.zeros(len(df), dtype=bool)
    for side, ok in (("bid", bid_ok), ("ask", ask_ok)):
        o, h, lo, c = (df[f"{side}_{k}"].to_numpy() for k in "ohlc")
        with np.errstate(invalid="ignore"):
            bad = (
                (h < np.maximum(o, c) - eps)
                | (lo > np.minimum(o, c) + eps)
                | (lo > h + eps)
                | (np.minimum(np.minimum(o, h), np.minimum(lo, c)) <= 0)
            )
        ohlc_bad |= ok & bad

    with np.errstate(invalid="ignore"):
        neg = both & np.any(
            [df[f"ask_{k}"].to_numpy() < df[f"bid_{k}"].to_numpy() - eps for k in "ohlc"], axis=0
        )
        spread = np.where(both, df["ask_c"].to_numpy() - df["bid_c"].to_numpy(), np.nan)

    day = _local_day(ts, tz)
    tick = 10 ** (-inst.price_decimals)
    spread_s = pd.Series(spread, index=day)
    day_median = spread_s.groupby(level=0).median()
    median_per_row = day_median.reindex(day).to_numpy() if len(day) else np.zeros(0)
    with np.errstate(invalid="ignore"):
        outlier = both & (spread > qs.spread_outlier_factor * np.maximum(median_per_row, tick))

    # Gaps: runs of expected minutes without a two-sided candle. ``[ws, we)`` is already
    # clipped to the downloaded range: minutes before/after the data are not "missing".
    exp = expected_minutes(spec, ws, we) if we > ws else pd.DatetimeIndex([], tz="UTC")
    present = np.isin(exp.asi8, ts.asi8[both])
    gap_runs = _runs(exp[~present])
    exp_day = _local_day(exp, tz)

    anomalies = [
        _anomaly_rows(gap_runs[gap_runs["minutes"] >= qs.gap_warn_minutes], "gap", tz),
        _anomaly_rows(_runs(ts[one_sided]), "one_sided", tz),
        _anomaly_rows(_runs(ts[ohlc_bad]), "ohlc_invalid", tz),
        _anomaly_rows(_runs(ts[neg]), "negative_spread", tz),
        _anomaly_rows(
            _runs(ts[outlier]),
            "spread_outlier",
            tz,
            f"spread > {qs.spread_outlier_factor:g} × médiane du jour",
        ),
        _anomaly_rows(_runs(ts[both & ~in_session]), "outside_session", tz),
    ]
    if len(issues):
        for kind, grp in issues.groupby("kind"):
            runs = _runs(pd.DatetimeIndex(grp["ts_utc"]).sort_values().unique())
            anomalies.append(_anomaly_rows(runs, str(kind), tz, str(grp["detail"].iloc[0])))

    gap_day = _local_day(pd.DatetimeIndex(gap_runs["start"]), tz)
    gap_len = gap_runs["minutes"].to_numpy()
    issue_day = (
        _local_day(pd.DatetimeIndex(issues["ts_utc"]), tz)
        if len(issues)
        else np.array([], dtype="datetime64[D]")
    )
    issue_kind = issues["kind"].to_numpy() if len(issues) else np.array([], dtype=object)

    frame = pd.DataFrame(
        {
            "expected": _count_by_day(np.ones(len(exp), dtype=bool), exp_day),
            "observed": _count_by_day(both & in_session, day),
            "observed_outside": _count_by_day(both & ~in_session, day),
            "one_sided": _count_by_day(one_sided, day),
            "ohlc_invalid": _count_by_day(ohlc_bad, day),
            "negative_spread": _count_by_day(neg, day),
            "spread_outliers": _count_by_day(outlier, day),
            "observed_both": _count_by_day(both, day),
            "max_gap": pd.Series(gap_len, index=gap_day).groupby(level=0).max()
            if len(gap_len)
            else pd.Series(dtype="int64"),
            "gaps_warn": _count_by_day(gap_len >= qs.gap_warn_minutes, gap_day),
            "exact_duplicates": _count_by_day(issue_kind == "exact_duplicate", issue_day),
            "conflicting_duplicates": _count_by_day(
                issue_kind == "conflicting_duplicate", issue_day
            ),
            "source_refused": _count_by_day(
                np.isin(issue_kind, ["source_missing", "source_unavailable"]), issue_day
            ),
            "median_spread": day_median / inst.pip_size
            if len(day_median)
            else pd.Series(dtype=float),
        }
    )
    frame = frame.fillna({c: 0 for c in frame.columns if c != "median_spread"})
    frame.index.name = "day"
    return _WindowStats(frame, anomalies, _heat(ts[both], spec.timezone), _heat(exp, spec.timezone))


def _classify(
    frame: pd.DataFrame,
    inst: Instrument,
    qs: QualitySettings,
    data_start: pd.Timestamp,
    data_end: pd.Timestamp,
) -> pd.DataFrame:
    tz = qs.day_timezone
    frame = frame[(frame["expected"] > 0) | (frame["observed_both"] > 0)].copy()
    days = frame.index.to_numpy().astype("datetime64[D]")
    start, end = _day_bounds(days, tz)
    frame["start_utc"], frame["end_utc"] = start, end
    int_cols = [
        "expected",
        "observed",
        "observed_outside",
        "one_sided",
        "ohlc_invalid",
        "negative_spread",
        "spread_outliers",
        "max_gap",
        "gaps_warn",
        "exact_duplicates",
        "conflicting_duplicates",
        "source_refused",
    ]
    frame[int_cols] = frame[int_cols].astype("int64")
    with np.errstate(invalid="ignore", divide="ignore"):
        frame["coverage"] = np.where(
            frame["expected"] > 0, frame["observed"] / frame["expected"].replace(0, np.nan), np.nan
        )

    py_days = [pd.Timestamp(d).date() for d in days]
    holidays = (
        underlying_holidays(inst.sessions.holiday_calendar, min(py_days), max(py_days))
        if py_days
        else set()
    )
    low_liq = set(qs.low_liquidity_days)
    statuses, reasons_col, hol_col, low_col = [], [], [], []
    for d, row in zip(py_days, frame.itertuples(), strict=True):
        is_hol = d in holidays
        is_low = f"{d.month:02d}-{d.day:02d}" in low_liq
        hol_col.append(is_hol)
        low_col.append(is_low)
        reasons: list[str] = []
        # A day cut by the data boundaries is partial only if quotes were expected in the cut.
        if row.start_utc < data_start and len(
            expected_minutes(inst.sessions, row.start_utc, min(data_start, row.end_utc))
        ):
            statuses.append("partial")
            reasons_col.append("jour commencé avant le début des données téléchargées")
            continue
        if row.end_utc > data_end and len(
            expected_minutes(inst.sessions, max(data_end, row.start_utc), row.end_utc)
        ):
            statuses.append("partial")
            reasons_col.append("jour non terminé à la date du téléchargement")
            continue
        if row.ohlc_invalid > qs.max_invalid_ohlc_minutes:
            reasons.append(f"{row.ohlc_invalid} bougie(s) incohérente(s)")
        if row.negative_spread > qs.max_negative_spread_minutes:
            reasons.append(f"{row.negative_spread} minute(s) avec ask < bid")
        if row.one_sided > qs.max_one_sided_minutes:
            reasons.append(f"{row.one_sided} minute(s) avec un seul côté (BID/ASK)")
        if row.conflicting_duplicates > 0:
            reasons.append(f"{row.conflicting_duplicates} doublon(s) contradictoire(s)")
        if row.source_refused > 0:
            reasons.append("données refusées par la source (Dukascopy) pour ce jour")
        both_total = row.observed + row.observed_outside
        if both_total and row.spread_outliers / both_total > qs.spread_outlier_max_share:
            reasons.append(
                f"spread aberrant sur {row.spread_outliers / both_total:.1%} des minutes"
            )
        if row.expected > 0 and not is_hol:
            if row.observed == 0:
                reasons.append("aucune donnée (jour manquant)")
            else:
                if row.coverage < qs.min_day_coverage:
                    reasons.append(f"couverture {row.coverage:.1%} < {qs.min_day_coverage:.0%}")
                if row.max_gap >= qs.gap_invalid_minutes:
                    reasons.append(f"trou de {row.max_gap} min")
        if reasons:
            status = "invalid"
        elif is_low and qs.exclude_low_liquidity:
            status = "excluded_low_liquidity"
        elif row.expected == 0:
            status = "off_session"
        elif is_hol:
            status = "holiday"
        else:
            status = "valid"
        if is_low and qs.exclude_low_liquidity:
            reasons.append("jour de faible liquidité")
        if is_hol:
            reasons.append("jour férié du sous-jacent")
        statuses.append(status)
        reasons_col.append("; ".join(reasons))
    frame["status"] = statuses
    frame["reasons"] = reasons_col
    frame["holiday"] = hol_col
    frame["low_liquidity"] = low_col
    frame = frame.reset_index()
    frame["day"] = pd.to_datetime(frame["day"]).dt.date
    return frame[DAY_COLUMNS]


# --- entry points -------------------------------------------------------------------
def run_quality(
    inst: Instrument,
    store: PriceStore,
    settings: QualitySettings,
    data_end: pd.Timestamp | None = None,
) -> QualityResult:
    """Quality control of all the stored data of ``inst`` (all periods, stats only)."""
    qs = settings.for_instrument(inst)
    tz = qs.day_timezone
    symbol = inst.symbol
    years = store.years(symbol)
    if not years:
        raise QualityError(f"{symbol} : aucune donnée téléchargée.")
    first, last, rows = store.span(symbol)
    if first is None:
        raise QualityError(f"{symbol} : fichiers Parquet vides.")
    if data_end is None:
        data_end = (last + MINUTE).ceil("D")
    first_local = first.tz_convert(tz).normalize()
    end_local = (data_end - pd.Timedelta(1, "ns")).tz_convert(tz).normalize() + pd.Timedelta(days=1)
    start_local_naive, end_local_naive = first_local.tz_localize(None), end_local.tz_localize(None)

    cache: dict[int, pd.DataFrame] = {}
    issues_all = store.read_issues(symbol)
    frames, anomalies = [], []
    obs_heat, exp_heat = np.zeros((7, 24)), np.zeros((7, 24))
    for year in range(start_local_naive.year, end_local_naive.year + 1):
        ws_naive = max(pd.Timestamp(year=year, month=1, day=1), start_local_naive)
        we_naive = min(pd.Timestamp(year=year + 1, month=1, day=1), end_local_naive)
        if we_naive <= ws_naive:
            continue
        ws = ws_naive.tz_localize(tz).tz_convert("UTC")
        we = we_naive.tz_localize(tz).tz_convert("UTC")
        parts = []
        for y in range(ws.year, (we - pd.Timedelta(1, "ns")).year + 1):
            if y not in cache:
                cache[y] = store.read_years(symbol, [y])
            parts.append(cache[y])
        for y in list(cache):
            if y < ws.year:
                del cache[y]
        df = pd.concat(parts, ignore_index=True)
        df = df[(df["ts_utc"] >= ws) & (df["ts_utc"] < we)].reset_index(drop=True)
        its = issues_all[(issues_all["ts_utc"] >= ws) & (issues_all["ts_utc"] < we)]
        data_start = first.floor("D")
        stats = _analyse_window(inst, qs, df, its, max(ws, data_start), min(we, data_end))
        frames.append(stats.days)
        anomalies.extend(a for a in stats.anomalies if not a.empty)
        obs_heat += stats.observed_heat
        exp_heat += stats.expected_heat

    days = _classify(pd.concat(frames), inst, qs, first.floor("D"), data_end)
    anomalies_df = (
        pd.concat(anomalies, ignore_index=True)
        .sort_values("start_utc", kind="stable")
        .reset_index(drop=True)
        if anomalies
        else pd.DataFrame(columns=ANOMALY_COLUMNS)
    )
    anomalies_df["day"] = pd.to_datetime(anomalies_df["day"]).dt.date
    weeks = max((data_end - first_local.tz_convert("UTC")) / pd.Timedelta(days=7), 1.0)
    heat_rows = [
        {
            "weekday": wd,
            "weekday_label": WEEKDAY_LABELS[wd],
            "hour": h,
            "observed_share": obs_heat[wd, h] / (weeks * 60),
            "expected_share": exp_heat[wd, h] / (weeks * 60),
        }
        for wd in range(7)
        for h in range(24)
    ]
    summary = _summary(inst, qs, store, days, anomalies_df, first, last, rows, data_end)
    return QualityResult(symbol, days, anomalies_df, pd.DataFrame(heat_rows), summary)


def _summary(inst, qs, store, days, anomalies, first, last, rows, data_end) -> dict:
    counts = days["status"].value_counts().to_dict()
    per_year = []
    years = pd.to_datetime(days["day"]).dt.year
    for year, grp in days.groupby(years):
        per_year.append(
            {
                "year": int(year),
                "days": len(grp),
                **{s: int((grp["status"] == s).sum()) for s in STATUS_LABELS},
                "median_coverage": float(grp["coverage"].median())
                if grp["coverage"].notna().any()
                else None,
                "median_spread": float(grp["median_spread"].median())
                if grp["median_spread"].notna().any()
                else None,
            }
        )
    return {
        "symbol": inst.symbol,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "btlab_version": __version__,
        "data_fingerprint": store.fingerprint(inst.symbol),
        "settings": qs.model_dump(mode="json"),
        "session_timezone": inst.sessions.timezone,
        "sessions_verified": inst.sessions.verified,
        "unit_name": inst.unit_name,
        "first_ts": first.isoformat(),
        "last_ts": last.isoformat(),
        "data_end": data_end.isoformat(),
        "rows": int(rows),
        "days_total": len(days),
        "status_counts": {k: int(counts.get(k, 0)) for k in STATUS_LABELS},
        "anomaly_counts": {k: int(v) for k, v in anomalies["kind"].value_counts().items()},
        "anomaly_minutes": {
            k: int(v) for k, v in anomalies.groupby("kind")["minutes"].sum().items()
        }
        if len(anomalies)
        else {},
        "per_year": per_year,
    }


def exclusions_from_days(days: pd.DataFrame) -> pd.DataFrame:
    excl = days[days["status"].isin(EXCLUDED_STATUSES)]
    return excl[["start_utc", "end_utc", "day", "status", "reasons"]].reset_index(drop=True)


# --- persistence --------------------------------------------------------------------
def quality_dir(root: Path, symbol: str) -> Path:
    return root / symbol


def save_quality(result: QualityResult, root: Path) -> Path:
    folder = quality_dir(root, result.symbol)
    folder.mkdir(parents=True, exist_ok=True)
    result.days.to_parquet(folder / "days.parquet", index=False)
    result.anomalies.to_parquet(folder / "anomalies.parquet", index=False)
    result.heatmap.to_parquet(folder / "heatmap.parquet", index=False)
    tmp = folder / "summary.json.tmp"
    tmp.write_text(json.dumps(result.summary, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(folder / "summary.json")
    return folder


def load_summary(root: Path, symbol: str) -> dict | None:
    path = quality_dir(root, symbol) / "summary.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def load_days(root: Path, symbol: str) -> pd.DataFrame:
    return pd.read_parquet(quality_dir(root, symbol) / "days.parquet")


def load_anomalies(root: Path, symbol: str) -> pd.DataFrame:
    return pd.read_parquet(quality_dir(root, symbol) / "anomalies.parquet")


def load_heatmap(root: Path, symbol: str) -> pd.DataFrame:
    return pd.read_parquet(quality_dir(root, symbol) / "heatmap.parquet")


def load_exclusions(root: Path, symbol: str) -> pd.DataFrame:
    return exclusions_from_days(load_days(root, symbol))


def quality_state(root: Path, store: PriceStore, symbol: str) -> str:
    """``missing`` (never run), ``stale`` (data changed since) or ``ok``."""
    summary = load_summary(root, symbol)
    if summary is None:
        return "missing"
    return "ok" if summary.get("data_fingerprint") == store.fingerprint(symbol) else "stale"


def first_valid_day(days: pd.DataFrame) -> date | None:
    valid = days[days["status"] == "valid"]
    return valid["day"].min() if len(valid) else None
