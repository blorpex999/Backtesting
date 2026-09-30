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
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
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
def parse_candles_csv(
    path: Path, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None
) -> tuple[pd.DataFrame, int]:
    """Read a dukascopy-node M1 CSV. Returns (frame, rows outside ``[start, end)``)."""
    cols = ["ts_utc", *_SIDE_COLS]
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame({c: pd.Series(dtype="float64") for c in cols}).astype(
            {"ts_utc": "datetime64[ns, UTC]"}
        ), 0
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
        self, instrument_id: str, side: str, start: pd.Timestamp, end: pd.Timestamp, dest: Path
    ) -> None:
        """Write the M1 candles of ``[start, end)`` for one side as CSV at ``dest``."""


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


class DukascopyNodeSource:
    """Runs the pinned dukascopy-node CLI with the local Node.js (installed in ``.tools``)."""

    name = "dukascopy-node"

    def __init__(self, tools_dir: Path, settings: DownloadSettings):
        self.tools_dir = tools_dir
        self.settings = settings

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
    ) -> list[str]:
        s = self.settings
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
            str(s.batch_size),
            "-bp",
            str(s.batch_pause_ms),
            "-r",
            str(s.retries),
            "-rp",
            str(s.retry_pause_ms),
            "-s",
        ]

    def fetch(
        self, instrument_id: str, side: str, start: pd.Timestamp, end: pd.Timestamp, dest: Path
    ) -> None:
        if start != start.normalize() or end != end.normalize():
            raise ValueError("dukascopy-node : les bornes doivent être des minuits UTC")
        self.ensure_installed()
        tmp_dir = dest.parent / ".tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{dest.stem}-{os.getpid()}"
        tmp_file = tmp_dir / f"{stem}.csv"
        tmp_file.unlink(missing_ok=True)
        cmd = self.command(instrument_id, side, start, end, tmp_dir, stem)
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
            raise DownloadError(
                f"dukascopy-node a échoué (code {proc.returncode}) pour {instrument_id} {side} "
                f"{start:%Y-%m-%d} → {end:%Y-%m-%d} :\n{tail}"
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp_file, dest)

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

    @property
    def ok(self) -> bool:
        return not self.failed


class Downloader:
    def __init__(
        self,
        source: M1Source,
        raw_dir: Path,
        store: PriceStore,
        settings: DownloadSettings,
        now: Callable[[], pd.Timestamp] = utc_now,
    ):
        self.source = source
        self.raw_root = raw_dir
        self.store = store
        self.settings = settings
        self.now = now

    def manifest_path(self, symbol: str) -> Path:
        return self.raw_root / symbol / "manifest.json"

    def raw_path(self, symbol: str, side: str, chunk: Chunk) -> Path:
        return self.raw_root / symbol / side / f"{chunk.key}.csv"

    def load_manifest(self, inst: Instrument) -> Manifest:
        return Manifest.load(self.manifest_path(inst.symbol), inst, self.source)

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
        log(f"{inst.symbol} : {len(chunks)} mois à télécharger ({span}).")
        touched_years: set[int] = set()
        for chunk in chunks:
            c_start, c_end = max(chunk.start, lo), min(chunk.end, hi)
            entry = {"start": c_start.isoformat(), "end": c_end.isoformat()}
            ok = True
            for side in SIDES:
                dest = self.raw_path(inst.symbol, side, chunk)
                try:
                    self.source.fetch(inst.dukascopy.instrument_id, side, c_start, c_end, dest)
                except DownloadError as err:
                    ok = False
                    report.failed.append((chunk.key, side, str(err)))
                    log(f"  ✗ {chunk.key} {side.upper()} : {err}")
                    break
                frame, _ = parse_candles_csv(dest, c_start, c_end)
                entry[side] = {"rows": len(frame)}
            if not ok:
                continue
            if min(entry["bid"]["rows"], entry["ask"]["rows"]) == 0 and len(
                expected_minutes(inst.sessions, c_start, c_end)
            ):
                msg = (
                    f"{inst.symbol} {chunk.key} : aucune bougie reçue (BID ou ASK) alors que "
                    "des cotations sont attendues"
                )
                report.warnings.append(msg)
                log(f"  ! {msg}")
            # Complete = the whole month is covered, from its start (or the start of the
            # instrument's history) to its end, and that end is in the past.
            entry["complete"] = bool(
                c_end == chunk.end <= cutoff and c_start == max(chunk.start, history_floor)
            )
            entry["downloaded_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            manifest.chunks[chunk.key] = entry
            manifest.save(self.manifest_path(inst.symbol))
            report.done.append(chunk.key)
            touched_years.add(chunk.year)
            log(
                f"  ✓ {chunk.key} : {entry['bid']['rows']} bougies BID, "
                f"{entry['ask']['rows']} bougies ASK"
            )
        if touched_years:
            report.years_built = self.build(inst, sorted(touched_years), log=log, report=report)
        return report

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
            bid = pd.concat(bids, ignore_index=True) if bids else parse_candles_csv(Path())[0]
            ask = pd.concat(asks, ignore_index=True) if asks else parse_candles_csv(Path())[0]
            prices, merge_issues = merge_sides(bid, ask)
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
