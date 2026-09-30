"""Configuration schemas: shipped configs load, invalid ones are refused clearly."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from btlab.config import ConfigError, parse_model
from btlab.data.instruments import Instrument, load_instrument_file, load_instruments
from btlab.data.settings import DataSettings, load_data_settings
from btlab.periods import PeriodsConfig, load_periods
from tests.conftest import REPO_ROOT

EXPECTED = {
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "USDCHF",
    "AUDUSD",
    "USDCAD",
    "EURGBP",
    "XAUUSD",
    "US100",
    "US500",
    "US30",
    "GER40",
    "USOIL",
    "UKOIL",
}


def test_shipped_configs_load():
    instruments = load_instruments(REPO_ROOT / "configs" / "instruments")
    assert set(instruments) == EXPECTED
    assert instruments["USDJPY"].pip_size == 0.01
    assert instruments["GER40"].quote_currency == "EUR"
    assert instruments["US100"].dukascopy.instrument_id == "usatechidxusd"
    assert all(not i.sessions.verified for i in instruments.values()), "horaires provisoires"
    load_periods(REPO_ROOT / "configs" / "periods.yaml")
    settings = load_data_settings(REPO_ROOT / "configs" / "data.yaml")
    assert settings.download.retries >= 1


def _eurusd() -> dict:
    path = REPO_ROOT / "configs" / "instruments" / "EURUSD.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"pip_size": -1}, "pip_size"),
        ({"quote_currency": "usd"}, "code ISO"),
        ({"symbol": "eur-usd"}, "majuscules"),
        ({"asset_class": "crypto"}, "asset_class"),
        ({"unknown_field": 1}, "unknown_field"),
    ],
)
def test_invalid_instrument_refused_with_clear_message(patch, message):
    data = {**_eurusd(), **patch}
    with pytest.raises(ConfigError) as err:
        parse_model(Instrument, data, "EURUSD.yaml")
    text = str(err.value)
    assert "Configuration invalide : EURUSD.yaml" in text
    assert message in text


@pytest.mark.parametrize(
    ("sessions_patch", "message"),
    [
        ({"timezone": "Europe/Pariss"}, "fuseau horaire inconnu"),
        ({"weekly_open": {"day": "dim", "time": "17:00"}}, "weekly_open.day"),
        ({"daily_breaks": [{"start": "17:00", "end": "17:00"}]}, "durée non nulle"),
        ({"closed_dates": ["25-12"]}, "MM-JJ"),
        ({"weekly_open": {"day": "fri", "time": "17:00"}}, "identiques"),
    ],
)
def test_invalid_sessions_refused(sessions_patch, message):
    data = _eurusd()
    data["sessions"] = {**data["sessions"], **sessions_patch}
    with pytest.raises(ConfigError, match=message):
        parse_model(Instrument, data, "EURUSD.yaml")


def test_symbol_must_match_file_name(tmp_path: Path):
    data = _eurusd()
    path = tmp_path / "GBPUSD.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="nom du fichier"):
        load_instrument_file(path)


def test_retries_zero_refused():
    # dukascopy-node hides network errors when retries = 0 (silent data loss).
    with pytest.raises(ConfigError, match=r"download\.retries"):
        parse_model(DataSettings, {"download": {"retries": 0}}, "data.yaml")


def test_periods_must_be_contiguous():
    bad = {
        "default": {
            "is": {"start": "2010-01-01", "end": "2020-01-01"},
            "oos": {"start": "2020-02-01", "end": "2024-01-01"},
            "holdout": {"start": "2024-01-01", "end": None},
        },
        "warmup_start": "2009-01-01",
    }
    with pytest.raises(ConfigError, match="sans trou ni chevauchement"):
        parse_model(PeriodsConfig, bad, "periods.yaml")


def test_period_overrides_per_instrument():
    cfg = parse_model(
        PeriodsConfig,
        {
            "default": {
                "is": {"start": "2010-01-01", "end": "2020-01-01"},
                "oos": {"start": "2020-01-01", "end": "2024-01-01"},
                "holdout": {"start": "2024-01-01", "end": None},
            },
            "warmup_start": "2009-01-01",
            "overrides": {
                "GER40": {
                    "is": {"start": "2014-01-01", "end": "2021-01-01"},
                    "oos": {"start": "2021-01-01", "end": "2024-01-01"},
                    "holdout": {"start": "2024-01-01", "end": None},
                }
            },
        },
        "periods.yaml",
    )
    assert str(cfg.for_symbol("GER40").is_end.date()) == "2021-01-01"
    assert str(cfg.for_symbol("EURUSD").is_end.date()) == "2020-01-01"
