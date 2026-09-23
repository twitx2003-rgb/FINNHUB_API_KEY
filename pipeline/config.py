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
    # Kronos (phase 5): sampled candle paths, reported only as a scenario range
    # of closes. "synthetic" = offline stand-in for --selftest and the tests.
    kronos_enabled: bool = True
    kronos_provider: str = "kronos"
    kronos_model: str = "NeoQuasar/Kronos-small"
    kronos_model_revision: str | None = None
    kronos_tokenizer: str = "NeoQuasar/Kronos-Tokenizer-base"
    kronos_tokenizer_revision: str | None = None
    kronos_device: str = "cpu"
    kronos_context: int = 512            # sessions of history; the model's limit is 512
    kronos_paths: int = 30
    kronos_temperature: float = 1.0
    kronos_top_p: float = 0.9
    kronos_batch_size: int = 32          # paths per model call (memory, not results)
    kronos_seed: int = 7
    kronos_max_invalid_pct: float = 20.0
    kronos_backtest_windows: int = 10    # 0 = no backtest
    kronos_backtest_paths: int = 20

    def __post_init__(self):
        object.__setattr__(self, "timesfm_series", tuple(self.timesfm_series))
        object.__setattr__(self, "interval", tuple(float(q) for q in self.interval))
        if self.timesfm_provider not in ("timesfm", "synthetic"):
            raise ConfigError(f"forecast.timesfm_provider must be 'timesfm' or 'synthetic', "
                              f"got '{self.timesfm_provider}'")
        if self.kronos_provider not in ("kronos", "synthetic"):
            raise ConfigError(f"forecast.kronos_provider must be 'kronos' or 'synthetic', "
                              f"got '{self.kronos_provider}'")
        if not 1 <= int(self.kronos_context) <= 512:
            raise ConfigError("forecast.kronos_context must be 1..512 (Kronos' trained context)")
        if int(self.kronos_paths) < 10 or int(self.kronos_backtest_paths) < 10:
            raise ConfigError("forecast.kronos_paths / kronos_backtest_paths must be >= 10: "
                              "a range needs many paths")
        if not 0 <= float(self.kronos_max_invalid_pct) <= 100:
            raise ConfigError("forecast.kronos_max_invalid_pct must be 0..100")
        if int(self.kronos_backtest_windows) < 0 or int(self.kronos_batch_size) < 1:
            raise ConfigError("forecast.kronos_backtest_windows must be >= 0 and "
                              "kronos_batch_size >= 1")
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
class DebateSettings:
    # Phase 5: bull vs bear + a moderator, written by Claude from the validated
    # brief only. "claude_code" = the local CLI on the user's subscription;
    # "anthropic" = the paid API (ANTHROPIC_API_KEY); "synthetic" = tests/selftest.
    enabled: bool = True
    provider: str = "claude_code"
    model: str = "sonnet"
    effort: str = "high"                  # low | medium | high | xhigh | max
    rounds: int = 2                       # 1 = openings only; 2 = + one rebuttal each
    max_tokens: int = 16000               # per answer
    max_cost_usd: float = 3.0             # stop before a call once the estimate reaches this
    fallbacks: bool = True                # server-side fallback model on a refusal

    def __post_init__(self):
        if self.provider not in ("claude_code", "anthropic", "synthetic"):
            raise ConfigError(f"debate.provider must be 'claude_code', 'anthropic' or 'synthetic', "
                              f"got '{self.provider}'")
        if self.effort not in ("low", "medium", "high", "xhigh", "max"):
            raise ConfigError(f"debate.effort must be low/medium/high/xhigh/max, got '{self.effort}'")
        if self.provider == "claude_code" and self.effort not in ("low", "medium", "high"):
            raise ConfigError(f"debate.effort '{self.effort}': Claude Code offers low/medium/high only")
        if self.provider == "anthropic" and not self.model.startswith("claude-"):
            raise ConfigError(f"debate.model '{self.model}' is not an API model id "
                              "(e.g. claude-opus-5, claude-sonnet-5)")
        if not 1 <= int(self.rounds) <= 3:
            raise ConfigError("debate.rounds must be 1..3")
        if int(self.max_tokens) < 1000 or float(self.max_cost_usd) <= 0:
            raise ConfigError("debate.max_tokens must be >= 1000 and max_cost_usd > 0")


@dataclass(frozen=True)
class Settings:
    root: Path
    cache_dir: Path
    log_dir: Path
    data: DataSettings
    validate: ValidateSettings
    forecast: ForecastSettings = field(default_factory=ForecastSettings)
    extract: ExtractSettings = field(default_factory=ExtractSettings)
    debate: DebateSettings = field(default_factory=DebateSettings)
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
                   secret: bool = False, ask=None, is_interactive=None) -> str:
        """Like env(), but when the value is missing and a person is at the
        terminal, ask for it once and save it to .env so it is not asked again.

        secret=True reads without echo (getpass) and never repeats the answer."""
        value = os.environ.get(name)
        if value:
            return value
        interactive = sys.stdin.isatty() if is_interactive is None else is_interactive
        if not interactive:
            raise ConfigError(f"{name} is not set in {self.root / '.env'}")
        if ask is None:
            import getpass
            ask = getpass.getpass if secret else input
        answer = ask(f"\n{question}\n> ").strip()
        if not answer or (must_contain and must_contain not in answer):
            shown = "the value typed" if secret else f"'{answer}'"
            raise ConfigError(f"{name}: {shown} is not valid — expected something containing "
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
    known_debate = {f.name for f in DebateSettings.__dataclass_fields__.values()}

    data_raw = _section(raw, "data")
    validate_raw = _section(raw, "validate")
    forecast_raw = _section(raw, "forecast")
    extract_raw = _section(raw, "extract")
    debate_raw = _section(raw, "debate")
    # Typos in config are silent bugs otherwise — surface them immediately.
    for name, given, known in (("data", data_raw, known_data), ("validate", validate_raw, known_validate),
                               ("forecast", forecast_raw, known_forecast),
                               ("extract", extract_raw, known_extract),
                               ("debate", debate_raw, known_debate)):
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
        debate=DebateSettings(**debate_raw),
        venvs=_section(raw, "venvs"),
        raw=raw,
    )
