"""Fallback provider: yfinance for prices/options, FRED for macro.

Enabled by config `data.fallback_provider`. It is a genuine fallback, not a
second opinion — stage 2 cross-checks against an independent source instead.
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from ..errors import ProviderError
from .base import MarketDataProvider

log = logging.getLogger(__name__)

# Pipeline series label -> FRED code. CPIAUCSL = CPI for All Urban Consumers,
# seasonally adjusted (same definition as LSE's usacsa); DGS10 = 10-Year Treasury.
FRED_CODES = {"CPI": "CPIAUCSL", "YIELD_10Y": "DGS10"}
_FRED_HISTORY_YEARS = 30


def fred_series(label: str) -> pd.DataFrame:
    """One pipeline macro series from FRED, as timestamp/series/source_symbol/value.

    Used both by this provider and as the per-series fallback when the primary
    provider's copy of a series is stale.
    """
    code = FRED_CODES.get(label)
    if code is None:
        raise ProviderError(f"no FRED code mapped for macro series '{label}'")
    try:
        from pandas_datareader import data as pdr
    except ImportError as exc:
        raise ProviderError("pandas-datareader is not installed — pip install pandas-datareader") from exc

    start = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=365 * _FRED_HISTORY_YEARS)).tz_localize(None)
    raw = pdr.DataReader(code, "fred", start)
    part = raw.reset_index()
    part.columns = ["timestamp", "value"]
    part["timestamp"] = pd.to_datetime(part["timestamp"], utc=True)
    part["value"] = pd.to_numeric(part["value"], errors="coerce")
    part["series"] = label
    part["source_symbol"] = code
    return part[["timestamp", "series", "source_symbol", "value"]].dropna().reset_index(drop=True)


def yahoo_next_earnings(symbol: str) -> dict[str, Any]:
    """Next earnings date(s) from Yahoo's calendar. LSE has no earnings dates.

    `Earnings Date` is a list: one date when confirmed, two (a window) when
    only estimated.
    """
    import yfinance as yf

    calendar = yf.Ticker(symbol).calendar or {}
    if "Earnings Date" not in calendar:
        raise ProviderError(f"yahoo calendar({symbol}): no 'Earnings Date'. "
                            f"Actual keys: {sorted(calendar)}")
    raw = calendar["Earnings Date"]
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    dates = sorted(pd.Timestamp(v).date().isoformat() for v in values if v is not None)
    if not dates:
        raise ProviderError(f"yahoo calendar({symbol}): 'Earnings Date' is empty")
    return {"dates": dates, "source": "yahoo"}


class YFinanceFredProvider(MarketDataProvider):
    name = "yfinance_fred"

    def __init__(self) -> None:
        try:
            import yfinance  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise ProviderError("yfinance is not installed — pip install yfinance") from exc

    def daily_ohlcv(self, symbol: str, lookback_days: int) -> pd.DataFrame:
        import yfinance as yf

        raw = yf.Ticker(symbol).history(period=f"{max(lookback_days, 5)}d", interval="1d")
        if raw is None or raw.empty:
            raise ProviderError(f"yfinance returned no daily bars for '{symbol}'")

        frame = raw.reset_index().rename(
            columns={"Date": "timestamp", "Open": "open", "High": "high",
                     "Low": "low", "Close": "close", "Volume": "volume"}
        )
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        frame["symbol"] = symbol
        keep = ["timestamp", "symbol", "open", "high", "low", "close", "volume"]
        return frame[keep].sort_values("timestamp").reset_index(drop=True)

    def options_chain(self, underlying: str, max_dte: int) -> pd.DataFrame:
        """yfinance exposes IV but not greeks — we return what exists and leave
        greeks null rather than computing a Black-Scholes number and passing it
        off as vendor data."""
        import yfinance as yf

        ticker = yf.Ticker(underlying)
        expiries = list(getattr(ticker, "options", []) or [])
        if not expiries:
            log.warning("yfinance: no option expiries for '%s'", underlying)
            return _empty()

        today = pd.Timestamp.now(tz="UTC").normalize()
        frames = []
        for expiry in expiries:
            dte = (pd.Timestamp(expiry, tz="UTC") - today).days
            if dte < 0 or dte > max_dte:
                continue
            chain = ticker.option_chain(expiry)
            for side, table in (("call", chain.calls), ("put", chain.puts)):
                if table is None or table.empty:
                    continue
                part = pd.DataFrame({
                    "underlying": underlying,
                    "contract": table.get("contractSymbol"),
                    "expiry": expiry,
                    "dte": dte,
                    "strike": table.get("strike"),
                    "type": side,
                    "last_price": table.get("lastPrice"),
                    "implied_volatility": table.get("impliedVolatility"),
                    "delta": pd.NA, "gamma": pd.NA, "theta": pd.NA, "vega": pd.NA,
                    "volume": table.get("volume"),
                    "open_interest": table.get("openInterest"),
                })
                frames.append(part)

        if not frames:
            return _empty()
        from ..contracts import normalize_options
        return normalize_options(pd.concat(frames, ignore_index=True))

    def market_cap_snapshot(self, symbol: str) -> dict[str, Any]:
        import yfinance as yf

        info = yf.Ticker(symbol).fast_info
        cap, price = info.market_cap, info.last_price
        if not cap or not price:
            raise ProviderError(f"yahoo fast_info({symbol}): market_cap={cap!r} last_price={price!r}")
        return {"market_cap": float(cap), "price": float(price), "as_of": None, "source": "yahoo"}

    def next_earnings(self, symbol: str) -> dict[str, Any]:
        return yahoo_next_earnings(symbol)

    def macro_series(self, cpi_series: str, yield_series: str) -> pd.DataFrame:
        """FRED copies of the pipeline's macro series. The config codes are LSE
        codes, so FRED_CODES maps by series label instead."""
        frames = []
        for label in FRED_CODES:
            try:
                frames.append(fred_series(label))
            except Exception as exc:  # noqa: BLE001 — network/vendor errors vary widely
                log.warning("FRED %s (%s) unavailable: %s", label, FRED_CODES[label], exc)
        frames = [f for f in frames if not f.empty]
        if not frames:
            return _empty_macro()
        return pd.concat(frames, ignore_index=True).sort_values(["series", "timestamp"]).reset_index(drop=True)


def _empty() -> pd.DataFrame:
    from .lse import _empty_options
    return _empty_options()


def _empty_macro() -> pd.DataFrame:
    from .lse import _empty_macro as e
    return e()
