"""Settings from config.yaml + .env, resolved once and passed around explicitly."""
from __future__ import annotations

import os
import sys
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
    # When TradingView is rate limited, its last answer may stand in if younger
    # than this (days). The close is always checked live.
    saved_market_cap_max_age_days: float = 7
    saved_earnings_max_age_days: float = 3

    def __post_init__(self):
        # YAML gives a list; keep the frozen dataclass hashable-friendly.
        object.__setattr__(self, "rate_limit_delays", tuple(float(d) for d in self.rate_limit_delays))
        if self.provider not in ("tradingview_mcp", "synthetic"):
            raise ConfigError(f"validate.provider must be 'tradingview_mcp' or 'synthetic', "
                              f"got '{self.provider}'")

    def tradingview_symbol(self, ticker: str) -> str:
        """NVDA -> NASDAQ:NVDA, unless tradingview_symbols maps it explicitly."""
        return self.tradingview_symbols.get(ticker) or f"{self.tradingview_exchange}:{ticker}"


# TimesFM is allowed only on series with trend and seasonality it can learn.
# Prices are refused on purpose: the report must never present a single-stock
# price path from a time-series model as a prediction (Kronos scenarios, phase
# 5, are labelled as scenario ranges instead).
FORECASTABLE = {"volume"}
NEVER_FORECAST = {"open", "high", "low", "close", "price"}


@dataclass(frozen=True)
class ForecastSettings:
    timesfm_enabled: bool = True
    # "timesfm" = the real model; "synthetic" = an offline stand-in used by
    # --selftest and the tests (never selected unless asked for).
    timesfm_provider: str = "timesfm"
    timesfm_checkpoint: str = "google/timesfm-3.0-pytorch"
    timesfm_revision: str | None = None
    timesfm_device: str = "cpu"
    timesfm_batch_size: int = 16
    timesfm_series: tuple[str, ...] = ("volume",)
    horizon: int = 10
    context_length: int = 512
    interval: tuple[float, float] = (0.1, 0.9)
    backtest_windows: int = 60
    backtest_horizon: int = 5

    def __post_init__(self):
        object.__setattr__(self, "timesfm_series", tuple(self.timesfm_series))
        object.__setattr__(self, "interval", tuple(float(q) for q in self.interval))
        if self.timesfm_provider not in ("timesfm", "synthetic"):
            raise ConfigError(f"forecast.timesfm_provider must be 'timesfm' or 'synthetic', "
                              f"got '{self.timesfm_provider}'")
        refused = set(self.timesfm_series) & NEVER_FORECAST
        if refused:
            raise ConfigError(f"forecast.timesfm_series: {sorted(refused)} refused — prices are "
                              "never forecast with TimesFM (see LICENSES.md, 'forecast honesty')")
        unknown = set(self.timesfm_series) - FORECASTABLE
        if unknown:
            raise ConfigError(f"forecast.timesfm_series: {sorted(unknown)} not supported; "
                              f"allowed: {sorted(FORECASTABLE)}")
        low, high = self.interval
        if not 0 < low < 0.5 < high < 1:
            raise ConfigError(f"forecast.interval must be (low, high) around 0.5, got {self.interval}")
        for name in ("horizon", "context_length", "backtest_windows", "backtest_horizon",
                     "timesfm_batch_size"):
            if int(getattr(self, name)) < 1:
                raise ConfigError(f"forecast.{name} must be >= 1")


@dataclass(frozen=True)
class ExtractSettings:
    # SEC EDGAR Form 4 (insider transactions). Needs SEC_USER_AGENT in .env.
    sec_enabled: bool = True
    sec_lookback_days: int = 180
    sec_max_filings: int = 150        # newest first; archive files are cached forever
    # Schedule 13G/13D (>5% holders). A holder may file only yearly, so look further back.
    sec_holders_lookback_days: int = 1095

    def __post_init__(self):
        if min(self.sec_lookback_days, self.sec_max_filings, self.sec_holders_lookback_days) < 1:
            raise ConfigError("extract.sec_* values must be >= 1")


@dataclass(frozen=True)
class Settings:
    root: Path
    cache_dir: Path
    log_dir: Path
    data: DataSettings
    validate: ValidateSettings
    forecast: ForecastSettings = field(default_factory=ForecastSettings)
    extract: ExtractSettings = field(default_factory=ExtractSettings)
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

    def env_or_ask(self, name: str, question: str, *, must_contain: str = "",
                   ask=input, is_interactive=None) -> str:
        """Like env(), but when the value is missing and a person is at the
        terminal, ask for it once and save it to .env so it is not asked again."""
        value = os.environ.get(name)
        if value:
            return value
        interactive = sys.stdin.isatty() if is_interactive is None else is_interactive
        if not interactive:
            raise ConfigError(f"{name} is not set in {self.root / '.env'}")
        answer = ask(f"\n{question}\n> ").strip()
        if not answer or (must_contain and must_contain not in answer):
            raise ConfigError(f"{name}: '{answer}' is not valid — expected something containing "
                              f"'{must_contain}'. Nothing was saved.")
        path = self.root / ".env"
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{prefix}{name}={answer}\n")
        os.environ[name] = answer
        print(f"Saved {name} to {path}\n")
        return answer


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
    known_forecast = {f.name for f in ForecastSettings.__dataclass_fields__.values()}
    known_extract = {f.name for f in ExtractSettings.__dataclass_fields__.values()}

    data_raw = _section(raw, "data")
    validate_raw = _section(raw, "validate")
    forecast_raw = _section(raw, "forecast")
    extract_raw = _section(raw, "extract")
    # Typos in config are silent bugs otherwise — surface them immediately.
    for name, given, known in (("data", data_raw, known_data), ("validate", validate_raw, known_validate),
                               ("forecast", forecast_raw, known_forecast),
                               ("extract", extract_raw, known_extract)):
        unknown = set(given) - known
        if unknown:
            raise ConfigError(f"config.yaml: unknown key(s) under '{name}': {sorted(unknown)}")

    return Settings(
        root=root,
        cache_dir=root / paths.get("cache", "cache"),
        log_dir=root / paths.get("logs", "logs"),
        data=DataSettings(**data_raw),
        validate=ValidateSettings(**validate_raw),
        forecast=ForecastSettings(**forecast_raw),
        extract=ExtractSettings(**extract_raw),
        venvs=_section(raw, "venvs"),
        raw=raw,
    )
