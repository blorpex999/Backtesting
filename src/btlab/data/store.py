"""Parquet price store: ``data/parquet/{SYMBOL}/{YYYY}.parquet`` (UTC years).

INTERNAL: this module reads data WITHOUT the period lock. Only data management
(download, build, quality control) may use it. Research code must go through
``btlab.data.loader``, which enforces the IS-only lock.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

PRICE_COLS = ["bid_o", "bid_h", "bid_l", "bid_c", "ask_o", "ask_h", "ask_l", "ask_c"]
VOLUME_COLS = ["bid_v", "ask_v"]
COLUMNS = ["ts_utc", *PRICE_COLS, *VOLUME_COLS]
SCHEMA = pa.schema(
    [pa.field("ts_utc", pa.timestamp("ns", tz="UTC"), nullable=False)]
    + [pa.field(c, pa.float64()) for c in PRICE_COLS + VOLUME_COLS]
)
ISSUE_COLUMNS = ["ts_utc", "side", "kind", "detail"]
ISSUE_SCHEMA = pa.schema(
    [
        pa.field("ts_utc", pa.timestamp("ns", tz="UTC")),
        pa.field("side", pa.string()),
        pa.field("kind", pa.string()),
        pa.field("detail", pa.string()),
    ]
)
_YEAR_FILE = re.compile(r"^(\d{4})\.parquet$")


def empty_prices() -> pd.DataFrame:
    return SCHEMA.empty_table().to_pandas()


def empty_issues() -> pd.DataFrame:
    return ISSUE_SCHEMA.empty_table().to_pandas()


def _atomic_write(table: pa.Table, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, path)


class PriceStore:
    def __init__(self, parquet_dir: Path):
        self.root = parquet_dir

    def symbol_dir(self, symbol: str) -> Path:
        return self.root / symbol

    def year_path(self, symbol: str, year: int) -> Path:
        return self.symbol_dir(symbol) / f"{year}.parquet"

    def issues_path(self, symbol: str, year: int) -> Path:
        return self.symbol_dir(symbol) / "_issues" / f"{year}.parquet"

    def years(self, symbol: str) -> list[int]:
        folder = self.symbol_dir(symbol)
        if not folder.is_dir():
            return []
        return sorted(int(m.group(1)) for p in folder.iterdir() if (m := _YEAR_FILE.match(p.name)))

    def symbols(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and self.years(p.name))

    # --- writing -------------------------------------------------------------------
    def write_year(
        self, symbol: str, year: int, prices: pd.DataFrame, issues: pd.DataFrame | None = None
    ) -> None:
        prices = prices.reindex(columns=COLUMNS)
        ts = pd.DatetimeIndex(prices["ts_utc"])
        if len(ts):
            if ts.tz is None or str(ts.tz) != "UTC":
                raise ValueError("ts_utc doit être en UTC")
            if not ts.is_monotonic_increasing or ts.has_duplicates:
                raise ValueError("ts_utc doit être strictement croissant (sans doublon)")
            if (ts.year != year).any():
                raise ValueError(f"des lignes n'appartiennent pas à l'année {year}")
        table = pa.Table.from_pandas(prices, schema=SCHEMA, preserve_index=False)
        _atomic_write(table, self.year_path(symbol, year))
        issues = empty_issues() if issues is None else issues.reindex(columns=ISSUE_COLUMNS)
        _atomic_write(
            pa.Table.from_pandas(issues, schema=ISSUE_SCHEMA, preserve_index=False),
            self.issues_path(symbol, year),
        )

    def write_frame(
        self, symbol: str, prices: pd.DataFrame, issues: pd.DataFrame | None = None
    ) -> list[int]:
        """Split a frame by UTC year and (over)write each year. Returns the years written."""
        prices = prices.sort_values("ts_utc", kind="stable").reset_index(drop=True)
        years = pd.DatetimeIndex(prices["ts_utc"]).year
        issue_years = (
            pd.DatetimeIndex(issues["ts_utc"]).year if issues is not None and len(issues) else None
        )
        written = []
        for year in sorted(set(years)):
            year_issues = None
            if issue_years is not None:
                year_issues = issues[issue_years == year]
            self.write_year(symbol, int(year), prices[years == year], year_issues)
            written.append(int(year))
        return written

    # --- reading -------------------------------------------------------------------
    def read_years(
        self,
        symbol: str,
        years: list[int],
        start: pd.Timestamp | None = None,
        end: pd.Timestamp | None = None,
        columns: list[str] | None = None,
    ) -> pd.DataFrame:
        filters = []
        if start is not None:
            filters.append(("ts_utc", ">=", start))
        if end is not None:
            filters.append(("ts_utc", "<", end))
        cols = None if columns is None else ["ts_utc", *[c for c in columns if c != "ts_utc"]]
        frames = []
        for year in years:
            path = self.year_path(symbol, year)
            if path.exists():
                table = pq.read_table(path, columns=cols, filters=filters or None)
                frames.append(table.to_pandas())
        if not frames:
            empty = empty_prices()
            return empty if cols is None else empty[cols]
        return pd.concat(frames, ignore_index=True)

    def read_issues(self, symbol: str, years: list[int] | None = None) -> pd.DataFrame:
        years = self.years(symbol) if years is None else years
        frames = [
            pq.read_table(p).to_pandas()
            for y in years
            if (p := self.issues_path(symbol, y)).exists()
        ]
        return pd.concat(frames, ignore_index=True) if frames else empty_issues()

    def span(self, symbol: str) -> tuple[pd.Timestamp | None, pd.Timestamp | None, int]:
        """First and last timestamp and row count, from the Parquet metadata only."""
        first = last = None
        rows = 0
        for year in self.years(symbol):
            meta = pq.ParquetFile(self.year_path(symbol, year)).metadata
            rows += meta.num_rows
            for rg in range(meta.num_row_groups):
                stats = meta.row_group(rg).column(0).statistics
                if stats is None or not stats.has_min_max:
                    continue
                lo, hi = pd.Timestamp(stats.min), pd.Timestamp(stats.max)
                lo = lo.tz_localize("UTC") if lo.tzinfo is None else lo.tz_convert("UTC")
                hi = hi.tz_localize("UTC") if hi.tzinfo is None else hi.tz_convert("UTC")
                first = lo if first is None or lo < first else first
                last = hi if last is None or hi > last else last
        return first, last, rows

    def fingerprint(self, symbol: str) -> str:
        """Cheap content stamp (names, sizes, mtimes) used to detect a stale QC."""
        h = hashlib.sha1()
        folder = self.symbol_dir(symbol)
        for year in self.years(symbol):
            for path in (self.year_path(symbol, year), self.issues_path(symbol, year)):
                if path.exists():
                    st = path.stat()
                    h.update(f"{path.relative_to(folder)}:{st.st_size}:{st.st_mtime_ns};".encode())
        return h.hexdigest()
