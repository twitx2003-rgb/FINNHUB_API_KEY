"""Settings from config.yaml + .env, resolved once and passed around explicitly."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .errors import ConfigError

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class DataSettings:
    provider: str = "lse"
    fallback_provider: str | None = "yfinance_fred"
    ohlcv_timeframe: str = "1d"
    ohlcv_lookback_days: int = 730
    max_staleness_days: int = 5
    drop_incomplete_session: bool = True
    market_timezone: str = "America/New_York"
    session_close: str = "16:15"
    options_max_dte: int = 90
    cpi_series: str = "usacsa"
    yield_series: str = "US10Y"
    fetch_options: bool = True
    fetch_macro: bool = True
    macro_max_age_daily: int = 7
    macro_max_age_monthly: int = 75
    macro_fallback: str | None = "fred"
    fetch_reference: bool = True
    # "regular": daily bars are the 09:30-16:00 session, rebuilt from intraday
    # candles. "extended": the vendor's own daily bar (LSE: 04:00-20:00).
    daily_session: str = "regular"

    def __post_init__(self):
        if self.daily_session not in ("regular", "extended"):
            raise ConfigError(f"data.daily_session must be 'regular' or 'extended', "
                              f"got '{self.daily_session}'")


@dataclass(frozen=True)
class ValidateSettings:
    provider: str = "tradingview_mcp"
    close_tolerance_pct: float = 0.5
    market_cap_tolerance_pct: float = 2.0
    earnings_tolerance_days: int = 1
    tradingview_url: str = "https://mcp.tradingview.com/mcp"
    tradingview_token_path: str = "~/.mrp/tv_tokens.json"
    tradingview_callback_host: str = "localhost"
    tradingview_callback_port: int = 8765
    tradingview_exchange: str = "NASDAQ"
    tradingview_symbols: dict[str, str] = field(default_factory=dict)
    rate_limit_delays: tuple[float, ...] = (5.0, 15.0, 45.0)

    def __post_init__(self):
        # YAML gives a list; keep the frozen dataclass hashable-friendly.
        object.__setattr__(self, "rate_limit_delays", tuple(float(d) for d in self.rate_limit_delays))
        if self.provider not in ("tradingview_mcp", "synthetic"):
            raise ConfigError(f"validate.provider must be 'tradingview_mcp' or 'synthetic', "
                              f"got '{self.provider}'")

    def tradingview_symbol(self, ticker: str) -> str:
        """NVDA -> NASDAQ:NVDA, unless tradingview_symbols maps it explicitly."""
        return self.tradingview_symbols.get(ticker) or f"{self.tradingview_exchange}:{ticker}"


@dataclass(frozen=True)
class Settings:
    root: Path
    cache_dir: Path
    log_dir: Path
    data: DataSettings
    validate: ValidateSettings
    venvs: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def env(self, name: str, *, required: bool = True) -> str | None:
        """Read a secret from the environment (.env is already loaded)."""
        value = os.environ.get(name)
        if required and not value:
            raise ConfigError(
                f"{name} is not set. Copy .env.example to .env and fill it in."
            )
        return value


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    section = raw.get(key) or {}
    if not isinstance(section, dict):
        raise ConfigError(f"config.yaml: '{key}' must be a mapping, got {type(section).__name__}")
    return section


def load_settings(config_path: Path | None = None, root: Path = ROOT) -> Settings:
    load_dotenv(root / ".env")

    path = config_path or (root / "config.yaml")
    if not path.exists():
        raise ConfigError(f"Missing config file: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml must be a mapping at the top level")

    paths = _section(raw, "paths")
    known_data = {f.name for f in DataSettings.__dataclass_fields__.values()}
    known_validate = {f.name for f in ValidateSettings.__dataclass_fields__.values()}

    data_raw = _section(raw, "data")
    validate_raw = _section(raw, "validate")
    # Typos in config are silent bugs otherwise — surface them immediately.
    for name, given, known in (("data", data_raw, known_data), ("validate", validate_raw, known_validate)):
        unknown = set(given) - known
        if unknown:
            raise ConfigError(f"config.yaml: unknown key(s) under '{name}': {sorted(unknown)}")

    return Settings(
        root=root,
        cache_dir=root / paths.get("cache", "cache"),
        log_dir=root / paths.get("logs", "logs"),
        data=DataSettings(**data_raw),
        validate=ValidateSettings(**validate_raw),
        venvs=_section(raw, "venvs"),
        raw=raw,
    )
