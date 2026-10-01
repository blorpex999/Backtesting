"""End-to-end through the CLI (fake source), the HTML report and the Streamlit pages."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from btlab.cli import app
from btlab.context import DataContext
from btlab.data import service
from btlab.data.loader import load_m1
from btlab.data.synthetic import synthetic_m1
from tests.conftest import FakeSource

runner = CliRunner()
REAL_MAKE_SOURCE = service.make_source


@pytest.fixture
def fake_source(ctx, monkeypatch) -> FakeSource:
    frame = synthetic_m1(ctx.instrument("GBPUSD"), "2019-12-01", "2020-01-10", seed=11, price=1.3)
    source = FakeSource({"gbpusd": frame})
    monkeypatch.setattr(service, "make_source", lambda _ctx: source)
    return source


def test_cli_instruments_lists_history_depth(project):
    result = runner.invoke(app, ["data", "instruments"])
    assert result.exit_code == 0, result.output
    assert "GER40" in result.output and "DEU.IDX/EUR" in result.output
    assert "2013-09-30" in result.output and "horaires provisoires" in result.output


def test_cli_download_qc_coverage_then_loader(project, fake_source):
    result = runner.invoke(
        app, ["data", "download", "GBPUSD", "--from", "2019-12-01", "--to", "2020-01-10"]
    )
    assert result.exit_code == 0, result.output
    assert "GBPUSD 2019.parquet" in result.output
    assert "jours valides" in result.output
    report = project.qc_reports / "GBPUSD.html"
    assert report.is_file()

    result = runner.invoke(app, ["data", "coverage"])
    assert result.exit_code == 0 and "GBPUSD" in result.output and "à jour" in result.output

    ctx = DataContext.load(project)
    df = load_m1("GBPUSD", "2019-12-02", "2019-12-20", ctx=ctx)
    assert not df.empty


def test_cli_download_reports_failure(project, fake_source):
    fake_source.fail_on = {("2019-12", "bid")}
    result = runner.invoke(
        app, ["data", "download", "GBPUSD", "--from", "2019-12-01", "--to", "2020-01-10", "--no-qc"]
    )
    assert result.exit_code == 1
    assert "échec simulé" in result.output or "échec simulé" in (result.stderr or "")


def test_cli_rejects_unknown_symbol(project):
    result = runner.invoke(app, ["data", "download", "FOO"])
    assert result.exit_code == 2


def test_html_report_is_self_contained_and_in_french(project, fake_source):
    runner.invoke(app, ["data", "download", "GBPUSD", "--from", "2019-12-01", "--to", "2020-01-10"])
    html = (project.qc_reports / "GBPUSD.html").read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>") and "lang='fr'" in html
    for section in (
        "Résumé",
        "Par année",
        "Jours exclus",
        "Anomalies",
        "Carte de couverture",
        "Hypothèse à remplacer",
        "Aucune donnée n'est réparée",
    ):
        assert section in html
    assert "<script src=" not in html  # plotly is inlined: the file works offline


# --- Streamlit pages (headless) ---------------------------------------------------------
APP_DIR = "src/btlab/ui"


def _app_test(page: str):
    from streamlit.testing.v1 import AppTest

    from tests.conftest import REPO_ROOT

    return AppTest.from_file(str(REPO_ROOT / APP_DIR / "views" / page), default_timeout=60)


def test_data_page_renders_without_data(project):
    at = _app_test("data.py").run()
    assert not at.exception, at.exception
    assert at.title[0].value == "Données"
    assert "Aucun contrôle qualité disponible pour l'instant." in [i.value for i in at.info]


def test_data_page_renders_with_quality_results(project, fake_source, monkeypatch):
    runner.invoke(app, ["data", "download", "GBPUSD", "--from", "2019-12-01", "--to", "2020-01-10"])
    monkeypatch.setattr(service, "make_source", REAL_MAKE_SOURCE)
    at = _app_test("data.py").run()
    assert not at.exception, at.exception
    assert any(m.label == "Valides" for m in at.metric)
    assert at.selectbox[0].value == "GBPUSD"


def test_home_page_shows_lock_and_journal(project):
    at = _app_test("home.py").run()
    assert not at.exception, at.exception
    table = at.table[0].value
    assert list(table["accès"]) == ["autorisé", "verrouillé", "verrouillé"]


def test_add_instrument_form_validates(project):
    at = _app_test("data.py").run()
    form_inputs = {w.label: w for w in at.text_input}
    form_inputs["Symbole (ex. XAGUSD)"].input("xagusd")
    form_inputs["Identifiant dukascopy-node (ex. xagusd)"].input("xagusd")
    form_inputs["Nom Dukascopy (ex. XAG/USD)"].input("XAG/USD")
    form_inputs["Devise de base (vide pour un indice)"].input("XAG")
    at.button(key="FormSubmitter:new_instrument-Enregistrer l'instrument").click().run()
    assert not at.exception, at.exception
    saved = project.instruments_dir / "XAGUSD.yaml"
    assert saved.is_file(), [e.value for e in at.error]
    assert "verified: false" in saved.read_text(encoding="utf-8")


def test_cli_update_and_build(project, fake_source):
    args = ["data", "download", "GBPUSD", "--from", "2019-12-01", "--to", "2020-01-10"]
    assert runner.invoke(app, args).exit_code == 0
    fake_source.calls.clear()
    result = runner.invoke(app, ["data", "update"])
    assert result.exit_code == 0, result.output
    assert "GBPUSD" in result.output
    result = runner.invoke(app, ["data", "build", "GBPUSD"])
    assert result.exit_code == 0 and "2019.parquet" in result.output


def test_cli_rate_limit_stops_remaining_symbols(project, fake_source, ctx, monkeypatch):
    import time

    monkeypatch.setattr(time, "sleep", lambda _: None)
    fake_source.frames["eurusd"] = synthetic_m1(
        ctx.instrument("EURUSD"), "2019-12-01", "2020-01-10"
    )
    fake_source.ip_block = 999
    args = ["data", "download", "GBPUSD", "EURUSD", "--from", "2019-12-01", "--to", "2020-01-10"]
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    out = result.output + (result.stderr or "")
    assert "HTTP 429" in out and "non traités cette fois-ci : EURUSD" in out
    assert not any(c[0] == "eurusd" for c in fake_source.calls)
