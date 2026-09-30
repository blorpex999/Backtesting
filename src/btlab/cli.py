"""Command line interface: ``btlab``."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Annotated

import pandas as pd
import typer

from btlab.config import ConfigError
from btlab.context import DataContext
from btlab.data import service
from btlab.data.download import DownloadError
from btlab.data.quality import QualityError

app = typer.Typer(
    help="backtest-lab : plateforme locale de backtest (recherche uniquement).",
    no_args_is_help=True,
)
data_app = typer.Typer(
    help="Données : téléchargement, contrôle qualité, couverture.", no_args_is_help=True
)
app.add_typer(data_app, name="data")

Symbols = Annotated[list[str] | None, typer.Argument(help="Symboles (ex. EURUSD GER40)")]


def _ctx() -> DataContext:
    try:
        return DataContext.load()
    except ConfigError as err:
        typer.secho(str(err), fg="red", err=True)
        raise typer.Exit(2) from None


def _resolve(
    ctx: DataContext, symbols: list[str] | None, all_: bool, default: list[str]
) -> list[str]:
    if all_:
        return sorted(ctx.instruments)
    chosen = [s.upper() for s in symbols] if symbols else default
    unknown = [s for s in chosen if s not in ctx.instruments]
    if unknown:
        typer.secho(f"Instrument(s) inconnu(s) : {', '.join(unknown)}", fg="red", err=True)
        raise typer.Exit(2)
    return chosen


def _date(value: str | None) -> pd.Timestamp | None:
    return pd.Timestamp(value, tz="UTC") if value else None


@data_app.command("setup")
def setup() -> None:
    """Vérifie Node.js et installe la version épinglée de dukascopy-node."""
    ctx = _ctx()
    source = service.make_source(ctx)
    try:
        typer.echo(f"Node.js {source.node_version()}")
        source.ensure_installed(log=typer.echo)
    except DownloadError as err:
        typer.secho(str(err), fg="red", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"dukascopy-node {source.version} prêt : {source.cli_js}")


@data_app.command("instruments")
def instruments() -> None:
    """Liste les instruments configurés et la profondeur d'historique annoncée."""
    ctx = _ctx()
    for s, inst in sorted(ctx.instruments.items()):
        flag = "" if inst.sessions.verified else "  [horaires provisoires]"
        typer.echo(
            f"{s:8} {inst.asset_class:7} {inst.dukascopy.name:18} "
            f"1re M1 : {inst.dukascopy.first_m1}{flag}"
        )


@data_app.command("download")
def download(
    symbols: Symbols = None,
    all_: Annotated[bool, typer.Option("--all", help="Tous les instruments configurés")] = False,
    start: Annotated[str | None, typer.Option("--from", help="Début (AAAA-MM-JJ, UTC)")] = None,
    end: Annotated[str | None, typer.Option("--to", help="Fin exclue (AAAA-MM-JJ, UTC)")] = None,
    force: Annotated[bool, typer.Option(help="Retélécharger les mois déjà complets")] = False,
    qc: Annotated[bool, typer.Option(help="Lancer le contrôle qualité ensuite")] = True,
) -> None:
    """Télécharge (incrémental) les bougies M1 BID et ASK, construit le Parquet, contrôle."""
    ctx = _ctx()
    chosen = _resolve(ctx, symbols, all_, [])
    if not chosen:
        typer.secho("Indiquez des symboles ou --all.", fg="red", err=True)
        raise typer.Exit(2)
    failed = False
    for symbol in chosen:
        try:
            report = service.download(
                ctx, symbol, _date(start), _date(end), force=force, qc=qc, log=typer.echo
            )
        except (DownloadError, QualityError) as err:
            typer.secho(f"{symbol} : {err}", fg="red", err=True)
            failed = True
            continue
        for w in report.warnings:
            typer.secho(f"Avertissement : {w}", fg="yellow")
        if not report.ok:
            failed = True
            typer.secho(
                f"{symbol} : {len(report.failed)} échec(s) ; relancez la commande pour "
                "reprendre là où elle s'est arrêtée.",
                fg="red",
                err=True,
            )
    raise typer.Exit(1 if failed else 0)


@data_app.command("update")
def update(qc: Annotated[bool, typer.Option(help="Contrôle qualité ensuite")] = True) -> None:
    """Met à jour tous les instruments déjà téléchargés."""
    ctx = _ctx()
    symbols = service.downloaded_symbols(ctx)
    if not symbols:
        typer.echo("Aucun instrument téléchargé pour l'instant (voir « btlab data download »).")
        raise typer.Exit(0)
    download(symbols, all_=False, start=None, end=None, force=False, qc=qc)


@data_app.command("build")
def build(symbols: Symbols = None) -> None:
    """Reconstruit le Parquet depuis les fichiers bruts (sans téléchargement)."""
    ctx = _ctx()
    for symbol in _resolve(ctx, symbols, False, service.downloaded_symbols(ctx)):
        service.make_downloader(ctx).build(ctx.instrument(symbol), log=typer.echo)


@data_app.command("qc")
def qc(
    symbols: Symbols = None,
    report: Annotated[bool, typer.Option(help="Écrire le rapport HTML")] = True,
) -> None:
    """Contrôle qualité (+ rapport HTML dans reports/qc/)."""
    ctx = _ctx()
    chosen = _resolve(ctx, symbols, False, ctx.store.symbols())
    if not chosen:
        typer.echo("Aucune donnée à contrôler.")
    failed = False
    for symbol in chosen:
        try:
            service.run_qc(ctx, symbol, report=report, log=typer.echo)
        except QualityError as err:
            typer.secho(str(err), fg="red", err=True)
            failed = True
    raise typer.Exit(1 if failed else 0)


@data_app.command("coverage")
def coverage() -> None:
    """Couverture par instrument et statut du contrôle qualité."""
    ctx = _ctx()
    table = service.coverage_table(ctx)
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        typer.echo(table.fillna("—").to_string(index=False))


@app.command("ui")
def ui(port: Annotated[int, typer.Option(help="Port local")] = 8501) -> None:
    """Lance l'interface Streamlit (uniquement sur 127.0.0.1)."""
    app_path = Path(__file__).parent / "ui" / "app.py"
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app_path),
        "--server.address",
        "127.0.0.1",
        "--server.port",
        str(port),
        "--browser.gatherUsageStats",
        "false",
        "--client.toolbarMode",
        "minimal",
    ]
    raise typer.Exit(subprocess.call(cmd))


if __name__ == "__main__":
    app()
