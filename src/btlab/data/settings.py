"""Data settings (``configs/data.yaml``): download parameters and quality thresholds."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from btlab.config import load_model
from btlab.data.instruments import Instrument, _check_mmdd, _check_timezone


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DownloadSettings(_Strict):
    tool: str = "dukascopy-node"
    version: str = "1.50.0"
    history_start: date = date(2009, 1, 1)
    # Dukascopy rate-limits its API (HTTP 429): stay gentle by default.
    batch_size: int = Field(default=2, gt=0)
    batch_pause_ms: int = Field(default=1500, ge=0)
    # >= 1 on purpose: with 0 retries dukascopy-node swallows network errors, writes an
    # empty file and exits with code 0 (observed with 1.50.0) - a silent data loss.
    retries: int = Field(default=3, ge=1)
    retry_pause_ms: int = Field(default=15000, ge=0)
    timeout_s: int = Field(default=3600, gt=0)
    # HTTP 429 handled on the Python side: wait (doubling each time), slow down, retry the
    # same month; give up the run after ``rate_limit_max_waits`` waits.
    rate_limit_wait_s: int = Field(default=60, gt=0)
    rate_limit_max_wait_s: int = Field(default=900, gt=0)
    rate_limit_max_waits: int = Field(default=6, ge=0)
    max_consecutive_failures: int = Field(default=3, ge=1)


class QualitySettings(_Strict):
    day_timezone: str = "Europe/Paris"
    gap_warn_minutes: int = Field(default=15, gt=0)
    gap_invalid_minutes: int = Field(default=60, gt=0)
    min_day_coverage: float = Field(default=0.90, ge=0, le=1)
    spread_outlier_factor: float = Field(default=10.0, gt=1)
    spread_outlier_max_share: float = Field(default=0.02, ge=0, le=1)
    max_negative_spread_minutes: int = Field(default=0, ge=0)
    max_one_sided_minutes: int = Field(default=5, ge=0)
    max_invalid_ohlc_minutes: int = Field(default=0, ge=0)
    low_liquidity_days: list[str] = Field(
        default_factory=lambda: ["12-24", "12-25", "12-31", "01-01"]
    )
    exclude_low_liquidity: bool = True

    _tz = field_validator("day_timezone")(_check_timezone)
    _dates = field_validator("low_liquidity_days")(_check_mmdd)

    def for_instrument(self, inst: Instrument) -> QualitySettings:
        overrides = {k: v for k, v in inst.quality.model_dump().items() if v is not None}
        return self.model_copy(update=overrides) if overrides else self


class DataSettings(_Strict):
    download: DownloadSettings = Field(default_factory=DownloadSettings)
    quality: QualitySettings = Field(default_factory=QualitySettings)


def load_data_settings(path: Path) -> DataSettings:
    return load_model(DataSettings, path)
