"""Deterministic synthetic provider.

Used by `run.py --selftest` and by the test suite, so the pipeline can be
exercised end to end with no API key and no network. It is registered under the
name "synthetic" but is never selected by config unless asked for explicitly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from .base import MarketDataProvider


class SyntheticProvider(MarketDataProvider):
    name = "synthetic"

    def __init__(self, bars: int = 120):   # enough history for the forecast backtest
        self.bars = bars

    def daily_ohlcv(self, symbol: str, lookback_days: int) -> pd.DataFrame:
        # End on yesterday, a session that is always complete, so the output does
        # not depend on whether the exchange happens to be open right now.
        end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        days = pd.date_range(end=end - timedelta(days=1), periods=self.bars, freq="D", tz="UTC")
        base = pd.Series(range(self.bars), dtype="float64") + 100.0
        # A weekly-ish wave, so volume forecasts and backtests have something to do.
        volume = 1_000_000.0 * (1 + 0.2 * np.sin(np.arange(self.bars) / 1.6))
        return pd.DataFrame({
            "timestamp": days,
            "symbol": symbol,
            "open": base,
            "high": base + 2.0,
            "low": base - 2.0,
            "close": base + 1.0,
            "volume": volume,
        })

    def options_chain(self, underlying: str, max_dte: int) -> pd.DataFrame:
        return pd.DataFrame({
            "underlying": [underlying] * 2,
            "contract": [f"{underlying}261218C00180000", f"{underlying}261218P00200000"],
            "expiry": ["2026-12-18"] * 2,
            "dte": [30, 30],
            "strike": [128.0, 132.0],
            "type": ["call", "put"],
            "last_price": [5.0, 4.0],
            "implied_volatility": [0.42, 0.45],
            "delta": [0.55, -0.45],
            "gamma": [0.01, 0.01],
            "theta": [-0.03, -0.02],
            "vega": [0.12, 0.11],
            "volume": [10.0, 20.0],
            "open_interest": [100.0, 200.0],
        })

    SHARES = 1_000_000_000.0

    def market_cap_snapshot(self, symbol: str) -> dict:
        price = float(self.daily_ohlcv(symbol, self.bars)["close"].iloc[-1])
        return {"market_cap": price * self.SHARES, "price": price, "as_of": None,
                "source": "synthetic"}

    def next_earnings(self, symbol: str) -> dict:
        day = (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat()
        return {"dates": [day], "source": "synthetic"}

    def macro_series(self, cpi_series: str, yield_series: str) -> pd.DataFrame:
        months = pd.date_range(end=datetime.now(timezone.utc), periods=4, freq="ME", tz="UTC")
        return pd.DataFrame({
            "timestamp": list(months) * 2,
            "series": ["CPI"] * 4 + ["YIELD_10Y"] * 4,
            "source_symbol": [cpi_series] * 4 + [yield_series] * 4,
            "value": [300.1, 301.0, 301.8, 302.5, 4.1, 4.2, 4.15, 4.3],
        })
