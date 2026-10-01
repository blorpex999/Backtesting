"""Dukascopy M1 BID/ASK download (via a pinned ``dukascopy-node``) and Parquet build.

Why ``dukascopy-node``: Dukascopy moved its data feed in 2026 (dukascopy-node 1.49+
reads a JSON API instead of the old ``.bi5`` files). A maintained client tracks such
changes; the version is pinned so that a silent format change cannot slip in.

Layout:
    data/raw/{SYMBOL}/{bid|ask}/{YYYY}-{MM}.csv   one file per month and side
    data/raw/{SYMBOL}/manifest.json               what was downloaded, when, how
    data/parquet/{SYMBOL}/{YYYY}.parquet          merged BID/ASK, UTC years

Only complete UTC days are downloaded (up to today 00:00 UTC). The current month
is marked partial and fetched again on the next update. Minutes without quotes
stay absent (no flat filler candles), so that gaps remain visible to the QC.

Resilience: a month is first fetched in one go. If that fails, it is fetched day by
day (reusing the month cache), so that one day refused by the server does not block
the whole month. Each refusal is qualified with a control request on a day known to
be served: if the control passes, only that day is refused (noted, retried at the
next run, declared unavailable after ``max_day_attempts`` runs); if the control is
refused too, the server is limiting the rate (pause, slow down, retry).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import pandas as pd

from btlab.data.instruments import Instrument
from btlab.data.sessions import expected_minutes
from btlab.data.settings import DownloadSettings
from btlab.data.store import ISSUE_COLUMNS, PriceStore, empty_issues

SIDES = ("bid", "ask")
CSV_HEADER = ["timestamp", "open", "high", "low", "close", "volume"]
_SIDE_COLS = ["o", "h", "l", "c", "v"]

Log = Callable[[str], None]


class DownloadError(RuntimeError):
    """Download failure; the message is meant for the user."""


class RateLimitError(DownloadError):
    """Dukascopy refused the requests because of their rate (HTTP 429)."""


def utc_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def utc_midnight(ts: pd.Timestamp) -> pd.Timestamp:
    return ts.tz_convert("UTC").normalize()


# --- month chunks ------------------------------------------------------------------
@dataclass(frozen=True, order=True)
class Chunk:
    year: int
    month: int

    @property
    def key(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def start(self) -> pd.Timestamp:
        return pd.Timestamp(year=self.year, month=self.month, day=1, tz="UTC")

    @property
    def end(self) -> pd.Timestamp:
        return self.start + pd.offsets.MonthBegin(1)

    @classmethod
    def from_key(cls, key: str) -> Chunk:
        y, m = key.split("-")
        return cls(int(y), int(m))


def month_chunks(start: pd.Timestamp, end: pd.Timestamp) -> list[Chunk]:
    """Months overlapping ``[start, end)``."""
    if end <= start:
        return []
    chunks = []
    cur = Chunk(start.year, start.month)
    while cur.start < end:
        chunks.append(cur)
        nxt = cur.end
        cur = Chunk(nxt.year, nxt.month)
    return chunks


# --- CSV parsing -------------------------------------------------------------------
def empty_candles() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_utc": pd.Series(dtype="datetime64[ns, UTC]"),
            **{c: pd.Series(dtype="float64") for c in _SIDE_COLS},
        }
    )


def parse_candles_csv(
    path: Path, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None
) -> tuple[pd.DataFrame, int]:
    """Read a dukascopy-node M1 CSV. Returns (frame, rows outside ``[start, end)``)."""
    if not path.is_file() or path.stat().st_size == 0:
        return empty_candles(), 0
    df = pd.read_csv(path, dtype={"timestamp": "int64"})
    if list(df.columns) != CSV_HEADER:
        raise DownloadError(
            f"Format CSV inattendu dans {path} : colonnes {list(df.columns)} "
            f"au lieu de {CSV_HEADER}. dukascopy-node a peut-être changé de format."
        )
    out = pd.DataFrame(
        {
            "ts_utc": pd.to_datetime(df["timestamp"], unit="ms", utc=True).dt.as_unit("ns"),
            "o": df["open"].astype("float64"),
            "h": df["high"].astype("float64"),
            "l": df["low"].astype("float64"),
            "c": df["close"].astype("float64"),
            "v": df["volume"].astype("float64"),
        }
    )
    keep = pd.Series(True, index=out.index)
    if start is not None:
        keep &= out["ts_utc"] >= start
    if end is not None:
        keep &= out["ts_utc"] < end
    return out[keep].reset_index(drop=True), int((~keep).sum())


def write_candles_csv(frame: pd.DataFrame, path: Path) -> None:
    """Write candles (``ts_utc, o, h, l, c, v``) in the dukascopy-node CSV layout."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if frame.empty:
        tmp.write_text("", encoding="utf-8")
    else:
        pd.DataFrame(
            {
                "timestamp": pd.DatetimeIndex(frame["ts_utc"]).as_unit("ms").asi8,
                "open": frame["o"],
                "high": frame["h"],
                "low": frame["l"],
                "close": frame["c"],
                "volume": frame["v"],
            }
        ).to_csv(tmp, index=False)
    os.replace(tmp, path)


# --- merging BID / ASK -------------------------------------------------------------
def _issues(ts, side: str, kind: str, detail) -> pd.DataFrame:
    ts = pd.DatetimeIndex(ts)
    return pd.DataFrame(
        {
            "ts_utc": ts,
            "side": side,
            "kind": kind,
            "detail": detail if isinstance(detail, list) else [detail] * len(ts),
        },
        columns=ISSUE_COLUMNS,
    )


def dedupe_side(df: pd.DataFrame, side: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Remove exact duplicates (reported) and drop conflicting duplicates (reported)."""
    issues = []
    exact = df.duplicated(keep="first")
    if exact.any():
        issues.append(
            _issues(df.loc[exact, "ts_utc"], side, "exact_duplicate", "ligne identique retirée")
        )
        df = df[~exact]
    conflict = df["ts_utc"].duplicated(keep=False)
    if conflict.any():
        ts = df.loc[conflict, "ts_utc"].drop_duplicates()
        issues.append(
            _issues(
                ts,
                side,
                "conflicting_duplicate",
                "même minute avec des prix différents : toutes les versions retirées",
            )
        )
        df = df[~conflict]
    out_issues = pd.concat(issues, ignore_index=True) if issues else empty_issues()
    return df.reset_index(drop=True), out_issues


def merge_sides(bid: pd.DataFrame, ask: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Outer-join BID and ASK candles on the minute. A missing side stays NaN (QC flags it)."""
    bid, bid_issues = dedupe_side(bid, "bid")
    ask, ask_issues = dedupe_side(ask, "ask")
    bid = bid.rename(columns={c: f"bid_{c}" for c in _SIDE_COLS})
    ask = ask.rename(columns={c: f"ask_{c}" for c in _SIDE_COLS})
    merged = pd.merge(bid, ask, on="ts_utc", how="outer", sort=True)
    issues = pd.concat([bid_issues, ask_issues], ignore_index=True)
    return merged.reset_index(drop=True), issues


# --- sources -----------------------------------------------------------------------
class M1Source(Protocol):
    name: str
    version: str

    def fetch(
        self,
        instrument_id: str,
        side: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
        dest: Path,
        cache_dir: Path | None = None,
    ) -> None:
        """Write the M1 candles of ``[start, end)`` for one side as CSV at ``dest``."""


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


class DukascopyNodeSource:
    """Runs the pinned dukascopy-node CLI with the local Node.js (installed in ``.tools``)."""

    name = "dukascopy-node"

    def __init__(
        self,
        tools_dir: Path,
        settings: DownloadSettings,
        sleep: Callable[[float], None] | None = None,
    ):
        self.tools_dir = tools_dir
        self.settings = settings
        self.sleep = sleep or time.sleep
        # Current throttle; reduced by ``slow_down`` after a rate-limit refusal.
        self.batch_size = settings.batch_size
        self.batch_pause_ms = settings.batch_pause_ms

    def slow_down(self) -> str:
        """Halve the parallel requests and double the pause (for the rest of the run)."""
        self.batch_size = max(1, self.batch_size // 2)
        self.batch_pause_ms = min(max(self.batch_pause_ms * 2, 1000), 10_000)
        return (
            f"{self.batch_size} requête(s) à la fois, pause de "
            f"{self.batch_pause_ms / 1000:g} s entre les lots"
        )

    @property
    def version(self) -> str:
        return self.settings.version

    @property
    def package_dir(self) -> Path:
        return self.tools_dir / f"dukascopy-node-{self.version}"

    @property
    def module_dir(self) -> Path:
        return self.package_dir / "node_modules" / "dukascopy-node"

    @property
    def cli_js(self) -> Path:
        return self.module_dir / "dist" / "cli" / "index.js"

    @staticmethod
    def node_executable() -> str:
        node = shutil.which("node")
        if not node:
            raise DownloadError(
                "Node.js est introuvable. Installez Node.js LTS (PowerShell : "
                "winget install OpenJS.NodeJS.LTS), rouvrez le terminal puis relancez."
            )
        return node

    def node_version(self) -> str:
        out = subprocess.run(
            [self.node_executable(), "--version"],
            capture_output=True,
            text=True,
            check=True,
            creationflags=_no_window(),
        )
        return out.stdout.strip()

    def is_installed(self) -> bool:
        return self.cli_js.is_file()

    def install(self, log: Log = print) -> None:
        self.node_executable()
        npm = shutil.which("npm")
        if not npm:
            raise DownloadError("npm est introuvable (il est normalement installé avec Node.js).")
        self.package_dir.mkdir(parents=True, exist_ok=True)
        log(f"Installation de dukascopy-node {self.version} dans {self.package_dir}…")
        proc = subprocess.run(
            [
                npm,
                "install",
                "--prefix",
                str(self.package_dir),
                f"dukascopy-node@{self.version}",
                "--no-audit",
                "--no-fund",
                "--omit=dev",
            ],
            capture_output=True,
            text=True,
            creationflags=_no_window(),
        )
        if proc.returncode != 0 or not self.is_installed():
            raise DownloadError(
                f"Échec de l'installation de dukascopy-node {self.version} :\n"
                f"{(proc.stderr or proc.stdout)[-2000:]}"
            )
        log("dukascopy-node installé.")

    def ensure_installed(self, log: Log = print) -> None:
        if not self.is_installed():
            self.install(log)

    def command(
        self,
        instrument_id: str,
        side: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
        out_dir: Path,
        stem: str,
        cache_dir: Path | None = None,
    ) -> list[str]:
        s = self.settings
        cache = ["-ch", "-chpath", str(cache_dir)] if cache_dir is not None else []
        return [
            self.node_executable(),
            str(self.cli_js),
            "-i",
            instrument_id,
            "-from",
            start.strftime("%Y-%m-%d"),
            "-to",
            end.strftime("%Y-%m-%d"),
            "-t",
            "m1",
            "-p",
            side,
            "-utc",
            "0",
            "-v",  # volumes: required for flat candles to be filtered out
            "-f",
            "csv",
            "-dir",
            str(out_dir),
            "-fn",
            stem,
            "-bs",
            str(self.batch_size),
            "-bp",
            str(self.batch_pause_ms),
            "-r",
            str(s.retries),
            "-rp",
            str(s.retry_pause_ms),
            # Optional per-month cache: after a failure, the days already fetched are not
            # requested again (the downloader deletes it once the month is complete).
            *cache,
            "-s",
        ]

    def fetch(
        self,
        instrument_id: str,
        side: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
        dest: Path,
        cache_dir: Path | None = None,
    ) -> None:
        if start != start.normalize() or end != end.normalize():
            raise ValueError("dukascopy-node : les bornes doivent être des minuits UTC")
        self.ensure_installed()
        tmp_dir = dest.parent / ".tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{dest.stem}-{os.getpid()}"
        tmp_file = tmp_dir / f"{stem}.csv"
        tmp_file.unlink(missing_ok=True)
        cmd = self.command(instrument_id, side, start, end, tmp_dir, stem, cache_dir)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.settings.timeout_s,
                creationflags=_no_window(),
            )
        except subprocess.TimeoutExpired:
            raise DownloadError(
                f"Délai dépassé ({self.settings.timeout_s} s) : {instrument_id} {side} "
                f"{start:%Y-%m-%d} → {end:%Y-%m-%d}"
            ) from None
        if proc.returncode != 0 or not tmp_file.exists():
            tmp_file.unlink(missing_ok=True)  # dukascopy-node leaves an empty file behind
            tail = (proc.stderr.strip() or proc.stdout.strip())[-1500:]
            what = f"{instrument_id} {side} {start:%Y-%m-%d} → {end:%Y-%m-%d}"
            if "status 429" in tail:
                raise RateLimitError(f"Dukascopy limite le débit (HTTP 429) : {what}")
            raise DownloadError(
                f"dukascopy-node a échoué (code {proc.returncode}) pour {what} :\n{tail}"
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp_file, dest)
        # Keep the same spacing between two runs of the CLI as between two batches.
        self.sleep(self.batch_pause_ms / 1000)

    def catalog(self) -> dict[str, dict]:
        """Instrument catalogue of dukascopy-node (ids, names, first available dates)."""
        cache = self.package_dir / "catalog.json"
        if cache.is_file():
            return json.loads(cache.read_text(encoding="utf-8"))
        self.ensure_installed()
        script = (
            "const m=require(process.argv[1]).instrumentMetaData;"
            "process.stdout.write(JSON.stringify(m));"
        )
        proc = subprocess.run(
            [self.node_executable(), "-e", script, str(self.module_dir)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            creationflags=_no_window(),
        )
        if proc.returncode != 0:
            raise DownloadError(f"Lecture du catalogue dukascopy-node impossible : {proc.stderr}")
        cache.write_text(proc.stdout, encoding="utf-8")
        return json.loads(proc.stdout)


# --- manifest ----------------------------------------------------------------------
@dataclass
class Manifest:
    symbol: str
    instrument_id: str
    source: str
    source_version: str
    chunks: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path, inst: Instrument, source: M1Source) -> Manifest:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(**data)
        return cls(inst.symbol, inst.dukascopy.instrument_id, source.name, source.version)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(self.__dict__, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)

    def is_complete(self, key: str) -> bool:
        return bool(self.chunks.get(key, {}).get("complete"))

    @property
    def cutoff(self) -> pd.Timestamp | None:
        """End of the last downloaded data (exclusive), if any."""
        ends = [pd.Timestamp(c["end"]) for c in self.chunks.values() if c.get("end")]
        return max(ends) if ends else None


@dataclass
class DownloadReport:
    symbol: str
    planned: list[str] = field(default_factory=list)
    done: list[str] = field(default_factory=list)
    failed: list[tuple[str, str, str]] = field(default_factory=list)
    years_built: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    aborted: str | None = None  # why the run stopped before the end, if it did
    rate_limited: bool = False
    retry_days: list[str] = field(default_factory=list)  # refused, retried next run
    unavailable_days: list[str] = field(default_factory=list)  # given up this run

    @property
    def ok(self) -> bool:
        return not self.failed and self.aborted is None


class _StopRun(DownloadError):
    """Stop the whole run (persistent rate limit, or too many failures in a row)."""

    def __init__(self, message: str, partial: dict | None = None):
        super().__init__(message)
        self.partial = partial


@dataclass
class _RunState:
    probe: tuple[pd.Timestamp, str]  # a (day, side) known to be served: control request
    error_streak: int = 0  # consecutive failed requests other than HTTP 429


def _first_wednesday(start: pd.Timestamp, end: pd.Timestamp) -> pd.Timestamp | None:
    days = pd.date_range(start, end, freq="D", inclusive="left")
    weds = days[days.dayofweek == 2]
    return weds[0] if len(weds) else None


class Downloader:
    def __init__(
        self,
        source: M1Source,
        raw_dir: Path,
        store: PriceStore,
        settings: DownloadSettings,
        now: Callable[[], pd.Timestamp] = utc_now,
        sleep: Callable[[float], None] | None = None,
    ):
        self.source = source
        self.raw_root = raw_dir
        self.store = store
        self.settings = settings
        self.now = now
        self.sleep = sleep or time.sleep

    # --- locations -------------------------------------------------------------
    def manifest_path(self, symbol: str) -> Path:
        return self.raw_root / symbol / "manifest.json"

    def raw_path(self, symbol: str, side: str, chunk: Chunk) -> Path:
        return self.raw_root / symbol / side / f"{chunk.key}.csv"

    def cache_dir(self, symbol: str, side: str, chunk: Chunk) -> Path:
        return self.raw_root / symbol / side / ".cache" / chunk.key

    def day_dir(self, symbol: str, side: str, chunk: Chunk) -> Path:
        return self.raw_root / symbol / side / f"{chunk.key}.days"

    def load_manifest(self, inst: Instrument) -> Manifest:
        return Manifest.load(self.manifest_path(inst.symbol), inst, self.source)

    # --- planning ---------------------------------------------------------------
    def cutoff(self) -> pd.Timestamp:
        """End of the last complete UTC day."""
        return utc_midnight(self.now())

    def bounds(
        self, inst: Instrument, start: pd.Timestamp | None, end: pd.Timestamp | None
    ) -> tuple[pd.Timestamp, pd.Timestamp]:
        floor = pd.Timestamp(max(self.settings.history_start, inst.dukascopy.first_m1), tz="UTC")
        lo = max(floor, start) if start is not None else floor
        hi = min(end, self.cutoff()) if end is not None else self.cutoff()
        return utc_midnight(lo), utc_midnight(hi)

    def plan(
        self,
        inst: Instrument,
        start: pd.Timestamp | None = None,
        end: pd.Timestamp | None = None,
        force: bool = False,
    ) -> list[Chunk]:
        lo, hi = self.bounds(inst, start, end)
        manifest = self.load_manifest(inst)
        chunks = month_chunks(lo, hi)
        if force:
            return chunks
        return [
            c
            for c in chunks
            if not (
                manifest.is_complete(c.key)
                and all(self.raw_path(inst.symbol, s, c).exists() for s in SIDES)
            )
        ]

    def _initial_probe(self, manifest: Manifest) -> tuple[pd.Timestamp, str]:
        """A day the server is known (or very likely) to serve, for control requests."""
        for key in sorted(manifest.chunks, reverse=True):
            entry = manifest.chunks[key]
            if entry.get("complete"):
                day = _first_wednesday(pd.Timestamp(entry["start"]), pd.Timestamp(entry["end"]))
                if day is not None:
                    return day, "bid"
        day = self.cutoff() - pd.Timedelta(days=2)
        while day.dayofweek != 2:
            day -= pd.Timedelta(days=1)
        return day, "bid"

    # --- download ---------------------------------------------------------------
    def download(
        self,
        inst: Instrument,
        start: pd.Timestamp | None = None,
        end: pd.Timestamp | None = None,
        force: bool = False,
        log: Log = print,
    ) -> DownloadReport:
        report = DownloadReport(inst.symbol)
        lo, hi = self.bounds(inst, start, end)
        chunks = self.plan(inst, start, end, force)
        report.planned = [c.key for c in chunks]
        if not chunks:
            log(f"{inst.symbol} : déjà à jour.")
            return report
        cutoff = self.cutoff()
        history_floor = self.bounds(inst, None, None)[0]
        manifest = self.load_manifest(inst)
        manifest.source, manifest.source_version = self.source.name, self.source.version
        span = f"{chunks[0].key} → {chunks[-1].key}"
        days = sum((min(c.end, hi) - max(c.start, lo)).days for c in chunks)
        log(
            f"{inst.symbol} : {len(chunks)} mois à télécharger ({span}), environ "
            f"{2 * days} requêtes (une par jour et par côté)."
        )
        state = _RunState(probe=self._initial_probe(manifest))
        touched_years: set[int] = set()
        for chunk in chunks:
            c_start, c_end = max(chunk.start, lo), min(chunk.end, hi)
            previous = manifest.chunks.get(chunk.key, {})
            if force or previous.get("start") != c_start.isoformat():
                previous = {}
                for side in SIDES:
                    shutil.rmtree(self.day_dir(inst.symbol, side, chunk), ignore_errors=True)
                    shutil.rmtree(self.cache_dir(inst.symbol, side, chunk), ignore_errors=True)
            entry: dict = {"start": c_start.isoformat(), "end": c_end.isoformat()}
            stop: _StopRun | None = None
            for side in SIDES:
                prev_side = dict(previous.get(side, {}))
                if previous.get("end") != c_end.isoformat():
                    # The range grew (current month): a month-mode side is fetched again;
                    # a day-mode side keeps its days and fetches only the new ones.
                    if prev_side.get("mode") == "day":
                        prev_side["complete"] = False
                    else:
                        prev_side = {}
                try:
                    entry[side] = self._download_side(
                        inst,
                        chunk,
                        side,
                        c_start,
                        c_end,
                        prev_side,
                        state,
                        log,
                        report,
                    )
                except _StopRun as err:
                    stop = err
                    if err.partial is not None:
                        entry[side] = err.partial
                    break
            covered = c_end == chunk.end <= cutoff and c_start == max(chunk.start, history_floor)
            entry["complete"] = bool(
                stop is None and covered and all(entry[s].get("complete") for s in SIDES)
            )
            entry["downloaded_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            if any(side in entry for side in SIDES):
                manifest.chunks[chunk.key] = entry
                manifest.save(self.manifest_path(inst.symbol))
                touched_years.add(chunk.year)
            if stop is not None:
                report.aborted = str(stop)
                report.failed.append((chunk.key, "", str(stop)))
                log(
                    f"  ■ Téléchargement de {inst.symbol} interrompu : {stop}. "
                    "Relancez plus tard : la reprise est automatique."
                )
                break
            self._check_month(inst, chunk, entry, c_start, c_end, log, report)
            report.done.append(chunk.key)
        if touched_years:
            report.years_built = self.build(inst, sorted(touched_years), log=log, report=report)
        return report

    def _check_month(self, inst, chunk, entry, c_start, c_end, log, report) -> None:
        rows = {s: entry[s]["rows"] for s in SIDES}
        pending = sum(len(entry[s].get("failed_days", {})) for s in SIDES)
        status = f" ; {pending} jour(s) en attente" if pending else ""
        log(f"  ✓ {chunk.key} : {rows['bid']} bougies BID, {rows['ask']} bougies ASK{status}")
        if min(rows.values()) == 0 and len(expected_minutes(inst.sessions, c_start, c_end)):
            msg = (
                f"{inst.symbol} {chunk.key} : aucune bougie reçue (BID ou ASK) alors que "
                "des cotations sont attendues"
            )
            report.warnings.append(msg)
            log(f"  ! {msg}")

    def _download_side(
        self, inst, chunk, side, start, end, previous: dict, state: _RunState, log, report
    ) -> dict:
        """One month and one side: in one go, or day by day if that fails."""
        dest = self.raw_path(inst.symbol, side, chunk)
        cache = self.cache_dir(inst.symbol, side, chunk)
        if previous.get("complete") and dest.exists():
            return previous
        if previous.get("mode") != "day":
            try:
                self.source.fetch(
                    inst.dukascopy.instrument_id, side, start, end, dest, cache_dir=cache
                )
            except DownloadError as err:
                report.rate_limited |= isinstance(err, RateLimitError)
                reason = "HTTP 429" if isinstance(err, RateLimitError) else "erreur"
                log(f"  … {chunk.key} {side.upper()} : mois refusé ({reason}), reprise par jour.")
            else:
                shutil.rmtree(cache, ignore_errors=True)
                state.error_streak = 0
                wednesday = _first_wednesday(start, end)
                if wednesday is not None:
                    state.probe = (wednesday, side)
                return {
                    "rows": len(parse_candles_csv(dest, start, end)[0]),
                    "mode": "month",
                    "complete": True,
                }
        return self._download_days(inst, chunk, side, start, end, previous, state, log, report)

    def _download_days(
        self, inst, chunk, side, start, end, previous: dict, state: _RunState, log, report
    ) -> dict:
        s = self.settings
        day_dir = self.day_dir(inst.symbol, side, chunk)
        cache = self.cache_dir(inst.symbol, side, chunk)
        day_dir.mkdir(parents=True, exist_ok=True)
        failed: dict = dict(previous.get("failed_days", {}))
        unavailable: dict = dict(previous.get("unavailable_days", {}))
        refused_now: dict[str, str] = {}
        days = pd.date_range(start, end, freq="D", inclusive="left")
        stop: _StopRun | None = None
        try:
            for day in days:
                key = f"{day:%Y-%m-%d}"
                if (day_dir / f"{key}.csv").exists() or key in unavailable:
                    continue
                error = self._fetch_day(
                    inst, side, day, day_dir / f"{key}.csv", cache, state, log, report
                )
                if error is None:
                    failed.pop(key, None)
                else:
                    refused_now[key] = error
        except _StopRun as err:
            stop = err
        for key, error in refused_now.items():
            attempts = failed.get(key, {}).get("attempts", 0) + 1
            if attempts >= s.max_day_attempts:
                failed.pop(key, None)
                unavailable[key] = f"{error}, {attempts} lancements"
                report.unavailable_days.append(f"{key} {side.upper()}")
                log(
                    f"  ✗ {key} {side.upper()} : refusé {attempts} fois ({error}) : déclaré "
                    "indisponible chez la source, exclu par le contrôle qualité."
                )
            else:
                failed[key] = {"attempts": attempts, "error": error}
                report.retry_days.append(f"{key} {side.upper()}")
                log(
                    f"  ! {key} {side.upper()} : refusé ({error}) ; nouvel essai au prochain "
                    f"lancement ({attempts}/{s.max_day_attempts})."
                )
        dest = self.raw_path(inst.symbol, side, chunk)
        frames = [parse_candles_csv(f, start, end)[0] for f in sorted(day_dir.glob("*.csv"))]
        frames = [f for f in frames if not f.empty]
        month = pd.concat(frames, ignore_index=True) if frames else empty_candles()
        write_candles_csv(month, dest)
        missing = [
            d
            for d in days
            if not (day_dir / f"{d:%Y-%m-%d}.csv").exists() and f"{d:%Y-%m-%d}" not in unavailable
        ]
        result = {
            "rows": len(month),
            "mode": "day",
            "complete": not missing,
            "failed_days": failed,
            "unavailable_days": unavailable,
        }
        if not missing:
            shutil.rmtree(day_dir, ignore_errors=True)
            shutil.rmtree(cache, ignore_errors=True)
        if stop is not None:
            stop.partial = result
            raise stop
        return result

    def _fetch_day(
        self, inst, side, day, dest: Path, cache: Path, state: _RunState, log, report
    ) -> str | None:
        """Fetch one day. Returns None if served, or why it was refused (to retry later).

        Raises ``_StopRun`` when the server keeps limiting the rate, or after too many
        failed requests in a row.
        """
        s = self.settings
        waits = 0
        while True:
            try:
                self.source.fetch(
                    inst.dukascopy.instrument_id,
                    side,
                    day,
                    day + pd.Timedelta(days=1),
                    dest,
                    cache_dir=cache,
                )
            except RateLimitError:
                report.rate_limited = True
                if not self._server_is_limiting(inst, state):
                    return "HTTP 429 sur ce jour seulement"
                if waits >= s.rate_limit_max_waits:
                    raise _StopRun(
                        f"Dukascopy limite toujours le débit (HTTP 429) après {waits} pauses"
                    ) from None
                delay = min(s.rate_limit_wait_s * 2**waits, s.rate_limit_max_wait_s)
                slow_down = getattr(self.source, "slow_down", None)
                slower = f" Débit réduit : {slow_down()}." if slow_down else ""
                log(
                    f"  … {day:%Y-%m-%d} {side.upper()} : Dukascopy limite le débit (HTTP 429). "
                    f"Pause de {delay} s puis nouvel essai ({waits + 1}/{s.rate_limit_max_waits})."
                    f"{slower}"
                )
                self.sleep(delay)
                waits += 1
                continue
            except DownloadError as err:
                state.error_streak += 1
                if state.error_streak >= s.max_consecutive_failures:
                    raise _StopRun(
                        f"{state.error_streak} requêtes de suite en échec (dernière erreur : "
                        f"{str(err).splitlines()[-1]})"
                    ) from None
                return str(err).splitlines()[-1][:200]
            state.error_streak = 0
            if day.dayofweek == 2:
                state.probe = (day, side)
            return None

    def _server_is_limiting(self, inst: Instrument, state: _RunState) -> bool:
        """Control request on a day known to be served: refused too = rate limit."""
        day, side = state.probe
        probe = self.raw_root / inst.symbol / ".probe.csv"
        try:
            self.source.fetch(
                inst.dukascopy.instrument_id, side, day, day + pd.Timedelta(days=1), probe
            )
        except DownloadError:
            return True
        finally:
            probe.unlink(missing_ok=True)
        return False

    def build(
        self,
        inst: Instrument,
        years: list[int] | None = None,
        log: Log = print,
        report: DownloadReport | None = None,
    ) -> list[int]:
        """Merge the raw monthly CSVs of each year into ``{YYYY}.parquet``."""
        manifest = self.load_manifest(inst)
        keys = sorted(manifest.chunks)
        all_years = sorted({Chunk.from_key(k).year for k in keys})
        years = all_years if years is None else [y for y in years if y in all_years]
        built = []
        for year in years:
            bids, asks, issues = [], [], []
            year_keys = [k for k in keys if k.startswith(f"{year:04d}-")]
            expected = [
                c.key
                for c in month_chunks(
                    max(
                        pd.Timestamp(year=year, month=1, day=1, tz="UTC"),
                        self.bounds(inst, None, None)[0],
                    ),
                    min(
                        pd.Timestamp(year=year + 1, month=1, day=1, tz="UTC"),
                        manifest.cutoff or self.cutoff(),
                    ),
                )
            ]
            missing = sorted(set(expected) - set(year_keys))
            if missing:
                msg = f"{inst.symbol} {year} : mois non téléchargés {', '.join(missing)}"
                log(f"  ! {msg}")
                if report is not None:
                    report.warnings.append(msg)
            for key in year_keys:
                chunk, entry = Chunk.from_key(key), manifest.chunks[key]
                c_start, c_end = pd.Timestamp(entry["start"]), pd.Timestamp(entry["end"])
                for side in SIDES:
                    info = entry.get(side, {})
                    for day, rec in info.get("failed_days", {}).items():
                        issues.append(
                            _issues(
                                [pd.Timestamp(day, tz="UTC")],
                                side,
                                "source_missing",
                                f"refusé : {rec['error']}",
                            )
                        )
                    for day, why in info.get("unavailable_days", {}).items():
                        issues.append(
                            _issues(
                                [pd.Timestamp(day, tz="UTC")],
                                side,
                                "source_unavailable",
                                f"abandonné : {why}",
                            )
                        )
                for side, bucket in (("bid", bids), ("ask", asks)):
                    frame, outside = parse_candles_csv(
                        self.raw_path(inst.symbol, side, chunk), c_start, c_end
                    )
                    bucket.append(frame)
                    if outside:
                        issues.append(
                            _issues(
                                [c_start],
                                side,
                                "outside_requested_range",
                                f"{outside} lignes hors de la plage demandée ignorées",
                            )
                        )
            bid = pd.concat(bids, ignore_index=True) if bids else empty_candles()
            ask = pd.concat(asks, ignore_index=True) if asks else empty_candles()
            prices, merge_issues = merge_sides(bid, ask)
            if prices.empty and not self.store.year_path(inst.symbol, year).exists():
                log(f"  → {inst.symbol} {year} : aucune bougie pour l'instant, rien à construire")
                continue
            all_issues = (
                pd.concat([*issues, merge_issues], ignore_index=True) if issues else merge_issues
            )
            self.store.write_year(inst.symbol, year, prices, all_issues)
            built.append(year)
            log(
                f"  → {inst.symbol} {year}.parquet : {len(prices)} minutes, "
                f"{len(all_issues)} anomalies de construction"
            )
        return built
