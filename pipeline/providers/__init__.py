"""Provider registry."""
from __future__ import annotations

from ..errors import ConfigError
from .base import MarketDataProvider


def get_provider(name: str, settings=None) -> MarketDataProvider:
    key = (name or "").strip().lower()
    if key == "lse":
        from .lse import LSEProvider
        api_key = settings.env("LSE_API_KEY") if settings else None
        return LSEProvider(api_key=api_key)
    if key in ("yfinance_fred", "yfinance", "yf"):
        from .yf_fred import YFinanceFredProvider
        return YFinanceFredProvider()
    if key == "synthetic":
        # Offline stand-in for --selftest and the test suite. Never produces
        # real market data, so it is only ever selected explicitly.
        from .synthetic import SyntheticProvider
        return SyntheticProvider()
    raise ConfigError(
        f"Unknown data provider '{name}'. Known: lse, yfinance_fred, synthetic"
    )
