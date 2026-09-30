"""Everything the data layer needs, loaded once from the project configuration."""

from __future__ import annotations

from dataclasses import dataclass

from btlab.data.instruments import Instrument, load_instruments
from btlab.data.settings import DataSettings, load_data_settings
from btlab.data.store import PriceStore
from btlab.paths import Paths
from btlab.periods import PeriodLock, PeriodsConfig, load_periods
from btlab.registry import Registry


@dataclass
class DataContext:
    paths: Paths
    instruments: dict[str, Instrument]
    periods: PeriodsConfig
    settings: DataSettings
    registry: Registry
    store: PriceStore
    lock: PeriodLock

    @classmethod
    def load(cls, paths: Paths | None = None) -> DataContext:
        paths = paths or Paths.from_root()
        periods = load_periods(paths.periods_file)
        registry = Registry(paths.registry_db)
        return cls(
            paths=paths,
            instruments=load_instruments(paths.instruments_dir),
            periods=periods,
            settings=load_data_settings(paths.data_settings_file),
            registry=registry,
            store=PriceStore(paths.parquet),
            lock=PeriodLock(periods, registry),
        )

    def instrument(self, symbol: str) -> Instrument:
        try:
            return self.instruments[symbol]
        except KeyError:
            known = ", ".join(sorted(self.instruments))
            raise KeyError(
                f"Instrument inconnu : {symbol}. Instruments configurés : {known}"
            ) from None
