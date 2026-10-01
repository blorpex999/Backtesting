"""Data operations shared by the CLI and the Streamlit pages."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pandas as pd

from btlab.context import DataContext
from btlab.data.download import Downloader, DownloadReport, DukascopyNodeSource, M1Source, Manifest
from btlab.data.quality import (
    EXCLUDED_STATUSES,
    QualityResult,
    load_summary,
    quality_state,
    run_quality,
    save_quality,
)
from btlab.report.qc_report import write_qc_report

Log = Callable[[str], None]

QC_STATE_LABELS = {"ok": "à jour", "stale": "à relancer", "missing": "jamais lancé"}


def make_source(ctx: DataContext) -> DukascopyNodeSource:
    return DukascopyNodeSource(ctx.paths.tools, ctx.settings.download)


def make_downloader(ctx: DataContext, source: M1Source | None = None) -> Downloader:
    return Downloader(source or make_source(ctx), ctx.paths.raw, ctx.store, ctx.settings.download)


def manifest_cutoff(ctx: DataContext, symbol: str) -> pd.Timestamp | None:
    path = ctx.paths.raw / symbol / "manifest.json"
    if not path.is_file():
        return None
    return Manifest.load(path, ctx.instrument(symbol), make_source(ctx)).cutoff


def run_qc(
    ctx: DataContext, symbol: str, report: bool = True, log: Log = print
) -> tuple[QualityResult, Path | None]:
    inst = ctx.instrument(symbol)
    result = run_quality(
        inst, ctx.store, ctx.settings.quality, data_end=manifest_cutoff(ctx, symbol)
    )
    save_quality(result, ctx.paths.quality)
    counts = result.summary["status_counts"]
    log(
        f"{symbol} : {counts['valid']} jours valides, {counts['invalid']} invalides, "
        f"{counts['excluded_low_liquidity']} exclus (faible liquidité), "
        f"{counts['holiday']} fériés."
    )
    path = None
    if report:
        source = f"{ctx.settings.download.tool} {ctx.settings.download.version}"
        path = write_qc_report(result, inst, source, ctx.paths.qc_reports)
        log(f"  rapport : {path}")
    return result, path


def download(
    ctx: DataContext,
    symbol: str,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    force: bool = False,
    qc: bool = True,
    log: Log = print,
    source: M1Source | None = None,
) -> DownloadReport:
    inst = ctx.instrument(symbol)
    report = make_downloader(ctx, source).download(inst, start, end, force=force, log=log)
    has_data = ctx.store.span(symbol)[2] > 0
    if (
        qc
        and has_data
        and (report.years_built or quality_state(ctx.paths.quality, ctx.store, symbol) != "ok")
    ):
        run_qc(ctx, symbol, log=log)
    return report


def downloaded_symbols(ctx: DataContext) -> list[str]:
    return [s for s in ctx.instruments if (ctx.paths.raw / s / "manifest.json").is_file()]


def coverage_table(ctx: DataContext) -> pd.DataFrame:
    rows = []
    for symbol, inst in sorted(ctx.instruments.items()):
        first, last, n = ctx.store.span(symbol)
        summary = load_summary(ctx.paths.quality, symbol)
        state = quality_state(ctx.paths.quality, ctx.store, symbol) if n else None
        counts = (summary or {}).get("status_counts", {})

        def count(key: str, counts: dict = counts) -> str:
            return str(counts[key]) if key in counts else "—"

        rows.append(
            {
                "symbole": symbol,
                "classe": inst.asset_class,
                "Dukascopy": inst.dukascopy.name,
                "1re M1 annoncée": inst.dukascopy.first_m1.isoformat(),
                "début stocké": first.strftime("%Y-%m-%d") if first is not None else "—",
                "fin stockée": last.strftime("%Y-%m-%d %H:%M") if last is not None else "—",
                "minutes": n,
                "contrôle qualité": QC_STATE_LABELS.get(state, "—") if state else "—",
                "jours valides": count("valid"),
                "jours invalides": count("invalid"),
                "jours exclus": str(sum(counts.get(k, 0) for k in EXCLUDED_STATUSES))
                if counts
                else "—",
                "horaires": "vérifiés" if inst.sessions.verified else "provisoires",
            }
        )
    return pd.DataFrame(rows)
