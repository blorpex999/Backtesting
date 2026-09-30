"""Instrument metadata (asset class, pip/point, quote currency, sessions, holidays).

Broker-dependent values (contract size, point value, lot step, commission) live in
broker profiles (milestone 2), not here.
"""

from __future__ import annotations

import re
from datetime import date, time
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from btlab.config import ConfigError, dump_yaml, load_model

AssetClass = Literal["forex", "index", "metal", "energy"]
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAY_INDEX: dict[str, int] = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}

_MMDD = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


class WeeklyTime(_Strict):
    day: Weekday
    time: time

    @property
    def minute_of_week(self) -> int:
        return WEEKDAY_INDEX[self.day] * 1440 + _minutes(self.time)


class TimeRange(_Strict):
    """Local time range; ``end <= start`` means it wraps past midnight."""

    start: time
    end: time

    @model_validator(mode="after")
    def _not_empty(self) -> TimeRange:
        if self.start == self.end:
            raise ValueError("une pause doit avoir une durée non nulle (début = fin)")
        return self

    @property
    def start_minute(self) -> int:
        return _minutes(self.start)

    @property
    def end_minute(self) -> int:
        return _minutes(self.end)


def _check_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"fuseau horaire inconnu : {value!r} (ex. Europe/Paris)") from None
    return value


def _check_mmdd(values: list[str]) -> list[str]:
    for v in values:
        if not _MMDD.match(v):
            raise ValueError(f"date annuelle invalide {v!r} : format attendu MM-JJ (ex. 12-25)")
    return values


class SessionSpec(_Strict):
    """Quoted hours, expressed in the local time of the market that sets them.

    Expressing them locally (e.g. America/New_York) keeps them right across daylight
    saving changes, including the weeks when Europe and the US are out of sync.
    """

    timezone: str
    weekly_open: WeeklyTime
    weekly_close: WeeklyTime
    daily_breaks: list[TimeRange] = Field(default_factory=list)
    closed_dates: list[str] = Field(default_factory=list, description="MM-JJ fermés chaque année")
    holiday_calendar: str | None = Field(
        default=None, description="Code exchange_calendars du sous-jacent (XNYS, XETR…)"
    )
    verified: bool = Field(
        default=False, description="False = horaires provisoires (hypothèse à remplacer)"
    )

    _tz = field_validator("timezone")(_check_timezone)
    _dates = field_validator("closed_dates")(_check_mmdd)

    @model_validator(mode="after")
    def _open_close_differ(self) -> SessionSpec:
        if self.weekly_open.minute_of_week == self.weekly_close.minute_of_week:
            raise ValueError("l'ouverture et la clôture hebdomadaires sont identiques")
        return self


class DukascopySpec(_Strict):
    instrument_id: str = Field(description="Identifiant dukascopy-node, ex. eurusd")
    name: str = Field(description="Nom Dukascopy, ex. EUR/USD")
    first_m1: date = Field(description="Première bougie M1 annoncée par dukascopy-node")

    @field_validator("instrument_id")
    @classmethod
    def _lower_id(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9]+", v):
            raise ValueError("identifiant dukascopy-node : minuscules et chiffres uniquement")
        return v


class QualityOverrides(_Strict):
    """Per-instrument overrides of the quality thresholds of ``configs/data.yaml``."""

    gap_warn_minutes: int | None = Field(default=None, gt=0)
    gap_invalid_minutes: int | None = Field(default=None, gt=0)
    min_day_coverage: float | None = Field(default=None, ge=0, le=1)
    spread_outlier_factor: float | None = Field(default=None, gt=1)
    spread_outlier_max_share: float | None = Field(default=None, ge=0, le=1)
    max_negative_spread_minutes: int | None = Field(default=None, ge=0)
    max_one_sided_minutes: int | None = Field(default=None, ge=0)
    max_invalid_ohlc_minutes: int | None = Field(default=None, ge=0)


class Instrument(_Strict):
    symbol: str
    description: str
    asset_class: AssetClass
    quote_currency: str
    base_currency: str | None = None
    unit_name: Literal["pip", "point"]
    pip_size: float = Field(gt=0, description="Taille du pip (forex) ou du point")
    price_decimals: int = Field(ge=0, le=8)
    dukascopy: DukascopySpec
    sessions: SessionSpec
    quality: QualityOverrides = Field(default_factory=QualityOverrides)
    notes: list[str] = Field(default_factory=list)

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Z0-9]{2,20}", v):
            raise ValueError("symbole : 2 à 20 caractères, majuscules et chiffres uniquement")
        return v

    @field_validator("quote_currency", "base_currency")
    @classmethod
    def _ccy(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"[A-Z]{3}", v):
            raise ValueError("devise : code ISO à 3 lettres majuscules (ex. USD)")
        return v

    @property
    def provisional_sessions(self) -> bool:
        return not self.sessions.verified


def instrument_path(instruments_dir: Path, symbol: str) -> Path:
    return instruments_dir / f"{symbol}.yaml"


def load_instrument_file(path: Path) -> Instrument:
    inst = load_model(Instrument, path)
    if inst.symbol != path.stem:
        raise ConfigError(
            f"Configuration invalide : {path}\n  - le symbole {inst.symbol!r} "
            f"doit correspondre au nom du fichier ({path.stem!r})"
        )
    return inst


def load_instruments(instruments_dir: Path) -> dict[str, Instrument]:
    files = sorted(instruments_dir.glob("*.yaml"))
    if not files:
        raise ConfigError(f"Aucun instrument configuré dans {instruments_dir}")
    return {inst.symbol: inst for inst in map(load_instrument_file, files)}


@lru_cache(maxsize=8)
def _cached(instruments_dir: str, _stamp: tuple) -> dict[str, Instrument]:
    return load_instruments(Path(instruments_dir))


def load_instruments_cached(instruments_dir: Path) -> dict[str, Instrument]:
    stamp = tuple((p.name, p.stat().st_mtime_ns) for p in sorted(instruments_dir.glob("*.yaml")))
    return _cached(str(instruments_dir), stamp)


def save_instrument(instruments_dir: Path, inst: Instrument, overwrite: bool = False) -> Path:
    path = instrument_path(instruments_dir, inst.symbol)
    if path.exists() and not overwrite:
        raise ConfigError(f"L'instrument {inst.symbol} existe déjà ({path})")
    dump_yaml(inst.model_dump(mode="json", exclude_defaults=False), path)
    return path
