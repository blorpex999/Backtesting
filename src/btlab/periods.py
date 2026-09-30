"""Research periods (IS / OOS / HOLDOUT) and the period lock.

The lock is enforced by the data loader itself: any request that touches OOS or
HOLDOUT data is refused unless it carries an access token that was granted *and*
journaled in the registry. Every refusal is journaled too.

Granting access (frozen strategy + explicit confirmation + contamination rules)
arrives with milestone 7; until then ``PeriodLock.request_access`` always refuses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from btlab.config import load_model
from btlab.registry import Registry

PROTECTED = ("oos", "holdout")
PERIOD_LABELS = {"is": "IS", "oos": "OOS", "holdout": "HOLDOUT", "warmup": "chauffe"}


class PeriodLockedError(PermissionError):
    """Access to a protected period (OOS / HOLDOUT) was refused."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DateRange(_Strict):
    start: date
    end: date | None = None  # exclusive; None = open-ended (until the end of the data)


class PeriodSet(_Strict):
    """Contiguous periods. Dates are UTC midnights, ``end`` is exclusive."""

    is_: DateRange = Field(alias="is")
    oos: DateRange
    holdout: DateRange

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def _contiguous(self) -> PeriodSet:
        if self.is_.end is None or self.oos.end is None:
            raise ValueError("IS et OOS doivent avoir une date de fin")
        if not (
            self.is_.start < self.is_.end == self.oos.start < self.oos.end == self.holdout.start
        ):
            raise ValueError(
                "les périodes doivent se suivre sans trou ni chevauchement : "
                "IS.fin = OOS.début et OOS.fin = HOLDOUT.début"
            )
        if self.holdout.end is not None and self.holdout.end <= self.holdout.start:
            raise ValueError("HOLDOUT : la fin doit être après le début")
        return self

    @property
    def is_end(self) -> pd.Timestamp:
        return _utc(self.is_.end)

    def bounds(self, name: str) -> tuple[pd.Timestamp, pd.Timestamp | None]:
        rng = {"is": self.is_, "oos": self.oos, "holdout": self.holdout}[name]
        return _utc(rng.start), (_utc(rng.end) if rng.end else None)

    def touched(self, start: pd.Timestamp, end: pd.Timestamp) -> list[str]:
        """Names of the periods overlapping ``[start, end)``."""
        names = []
        for name in ("is", "oos", "holdout"):
            p_start, p_end = self.bounds(name)
            if end > p_start and (p_end is None or start < p_end):
                names.append(name)
        return names


class PeriodsConfig(_Strict):
    default: PeriodSet
    warmup_start: date
    overrides: dict[str, PeriodSet] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _warmup(self) -> PeriodsConfig:
        if self.warmup_start > self.default.is_.start:
            raise ValueError("warmup_start doit précéder le début de l'IS")
        return self

    def for_symbol(self, symbol: str) -> PeriodSet:
        return self.overrides.get(symbol, self.default)


def load_periods(path: Path) -> PeriodsConfig:
    return load_model(PeriodsConfig, path)


def _utc(d: date) -> pd.Timestamp:
    return pd.Timestamp(d).tz_localize("UTC")


def to_utc(value) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


@dataclass(frozen=True)
class PeriodAccess:
    """Access token for a protected period. Only valid if journaled as granted."""

    token: str
    symbol: str
    period: str


def _fmt(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m-%d %H:%M UTC")


class PeriodLock:
    def __init__(self, periods: PeriodsConfig, registry: Registry):
        self.periods = periods
        self.registry = registry

    def check(
        self,
        symbol: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
        access: PeriodAccess | None = None,
        caller: str = "",
    ) -> list[str]:
        """Raise ``PeriodLockedError`` unless ``[start, end)`` is allowed.

        Returns the names of the periods the request touches.
        """
        start, end = to_utc(start), to_utc(end)
        if end <= start:
            raise ValueError(f"plage vide ou inversée : {_fmt(start)} → {_fmt(end)}")
        pset = self.periods.for_symbol(symbol)
        touched = pset.touched(start, end)
        protected = [p for p in touched if p in PROTECTED]
        if not protected:
            return touched

        labels = " et ".join(PERIOD_LABELS[p] for p in protected)
        if access is None:
            reason = "aucun jeton d'accès"
        else:
            reason = self._verify(access, symbol, protected, start, end, pset)
            if reason is None:
                return touched
        self.registry.log_period_access(
            symbol=symbol,
            period="+".join(protected),
            start=start.isoformat(),
            end=end.isoformat(),
            status="refused",
            reason=reason,
            token=access.token if access else None,
            caller=caller,
        )
        last_is_day = (pset.is_end - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        raise PeriodLockedError(
            f"Accès refusé ({reason}) : la plage {_fmt(start)} → {_fmt(end)} touche la période "
            f"{labels} de {symbol}. Le chargeur n'autorise que l'IS "
            f"(jusqu'au {last_is_day} inclus). "
            "L'accès OOS/HOLDOUT exige une stratégie figée et une confirmation explicite "
            "(jalon 7). Tentative journalisée dans le registre."
        )

    def _verify(
        self,
        access: PeriodAccess,
        symbol: str,
        protected: list[str],
        start: pd.Timestamp,
        end: pd.Timestamp,
        pset: PeriodSet,
    ) -> str | None:
        grant = self.registry.find_grant(access.token)
        if grant is None:
            return "jeton inconnu du registre"
        if grant["symbol"] != symbol or access.symbol != symbol:
            return "jeton émis pour un autre instrument"
        if protected != [grant["period"]] or access.period != grant["period"]:
            return (
                f"jeton limité à la période {PERIOD_LABELS.get(grant['period'], grant['period'])}"
            )
        _, p_end = pset.bounds(grant["period"])
        if p_end is not None and end > p_end:
            return "plage au-delà de la période autorisée"
        return None

    def request_access(self, symbol: str, period: str, caller: str = "") -> PeriodAccess:
        reason = "validation OOS/HOLDOUT non disponible avant le jalon 7"
        self.registry.log_period_access(
            symbol=symbol,
            period=period,
            start=None,
            end=None,
            status="refused",
            reason=reason,
            token=None,
            caller=caller,
        )
        raise PeriodLockedError(
            f"Accès {PERIOD_LABELS.get(period, period)} refusé pour {symbol} : {reason}. "
            "Tentative journalisée."
        )
