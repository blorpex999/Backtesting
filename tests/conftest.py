"""Shared fixtures: an isolated project copy per test and a fake Dukascopy source."""

from __future__ import annotations

import shutil
from pathlib import Path

import pandas as pd
import pytest

from btlab.context import DataContext
from btlab.data.download import DownloadError, RateLimitError
from btlab.data.synthetic import synthetic_m1
from btlab.paths import DATA_ENV, ROOT_ENV, Paths

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Paths:
    """A throw-away project root with the real configs: tests never touch the real
    registry, data or reports."""
    root = tmp_path / "project"
    shutil.copytree(REPO_ROOT / "configs", root / "configs")
    (root / "pyproject.toml").write_text("[project]\nname = 'test'\n", encoding="utf-8")
    monkeypatch.setenv(ROOT_ENV, str(root))
    monkeypatch.delenv(DATA_ENV, raising=False)
    return Paths.from_root(root)


@pytest.fixture
def ctx(project: Paths) -> DataContext:
    return DataContext.load(project)


def write_dukascopy_csv(frame: pd.DataFrame, side: str, path: Path) -> None:
    """Write candles in the exact CSV layout produced by dukascopy-node (ms timestamps)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if frame.empty:
        path.write_text("", encoding="utf-8")  # dukascopy-node writes an empty file
        return
    ms = pd.DatetimeIndex(frame["ts_utc"]).as_unit("ms").asi8
    out = pd.DataFrame(
        {
            "timestamp": ms,
            "open": frame[f"{side}_o"],
            "high": frame[f"{side}_h"],
            "low": frame[f"{side}_l"],
            "close": frame[f"{side}_c"],
            "volume": frame[f"{side}_v"],
        }
    )
    out.to_csv(path, index=False)


class FakeSource:
    """In-memory Dukascopy: serves synthetic candles, can fail on demand, records calls."""

    name = "fake-dukascopy"
    version = "0.0"

    def __init__(self, frames: dict[str, pd.DataFrame]):
        self.frames = frames  # instrument_id -> full BID/ASK frame
        self.calls: list[tuple[str, str, pd.Timestamp, pd.Timestamp]] = []
        self.fail_on: set[tuple[str, str]] = set()  # (YYYY-MM, side)
        self.rate_limit: dict[tuple[str, str], int] = {}  # (YYYY-MM, side) -> 429 count
        self.slow_downs = 0

    def slow_down(self) -> str:
        self.slow_downs += 1
        return "ralenti"

    def fetch(self, instrument_id, side, start, end, dest: Path) -> None:
        self.calls.append((instrument_id, side, start, end))
        key = (f"{start:%Y-%m}", side)
        if self.rate_limit.get(key, 0) > 0:
            self.rate_limit[key] -= 1
            raise RateLimitError(f"Dukascopy limite le débit (HTTP 429) : {key}")
        if key in self.fail_on:
            raise DownloadError(f"échec simulé {start:%Y-%m} {side}")
        frame = self.frames[instrument_id]
        part = frame[(frame["ts_utc"] >= start) & (frame["ts_utc"] < end)]
        write_dukascopy_csv(part, side, dest)


@pytest.fixture
def eurusd_frame(ctx: DataContext) -> pd.DataFrame:
    return synthetic_m1(ctx.instrument("EURUSD"), "2019-11-01", "2020-03-01", seed=1)
