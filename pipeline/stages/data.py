"""Stage 1 — data.

Pulls daily OHLCV, the option chain (greeks + IV) and macro series, validates
them against their contracts, and writes three Parquet artifacts.

This is the only stage that touches a market-data vendor. Everything downstream
reads these files.
"""
from __future__ import annotations

import logging

import pandas as pd

from .. import contracts
from ..cache import RunContext
from ..errors import ProviderError
from ..macro_freshness import refresh_stale_series
from ..market_hours import drop_incomplete_session
from ..providers import get_provider
from ..providers.base import assert_fresh
from .base import Stage, StageResult

log = logging.getLogger(__name__)


class DataStage(Stage):
    name = "data"
    requires_validation = False  # this stage produces what validation checks

    def run(self, ctx: RunContext) -> StageResult:
        cfg = ctx.settings.data
        provider, provider_name = self._resolve_provider(ctx)

        # ---- OHLCV (mandatory) -------------------------------------------
        ohlcv = provider.daily_ohlcv(ctx.ticker, cfg.ohlcv_lookback_days)
        dropped = None
        if cfg.drop_incomplete_session:
            ohlcv, dropped = drop_incomplete_session(
                ohlcv, market_tz=cfg.market_timezone, session_close=cfg.session_close
            )
            if dropped:
                log.warning(
                    "dropped in-progress session bar %s (market time %s, last trade %.2f, "
                    "volume so far %.0f) — it is not a close yet",
                    dropped.date, dropped.market_time, dropped.close, dropped.volume,
                )
        contracts.OHLCV.validate(ohlcv)
        contracts.assert_ohlcv_sane(ohlcv)
        assert_fresh(ohlcv, "timestamp", cfg.max_staleness_days,
                     context=f"{provider_name} daily_ohlcv({ctx.ticker})")
        ctx.write_parquet("data_ohlcv", ohlcv, contracts.OHLCV)

        artifacts = ["data_ohlcv"]
        details: dict = {"provider": provider_name, "ohlcv_rows": len(ohlcv)}
        if dropped:
            details["dropped_incomplete_bar"] = dropped.as_dict()

        # ---- options (optional) ------------------------------------------
        if cfg.fetch_options:
            options = contracts.normalize_options(
                provider.options_chain(ctx.ticker, cfg.options_max_dte)
            )
            ctx.write_parquet("data_options", options, contracts.OPTIONS_CHAIN)
            artifacts.append("data_options")
            details["options_rows"] = len(options)
            details["options_summary"] = summarize_options(
                options, spot=float(ohlcv["close"].iloc[-1])
            )
            if not options.empty:
                summary = details["options_summary"]
                log.info(
                    "options: %d contracts, %d expiries, ATM IV ~30d %s, IV median %s "
                    "(%d%% null), delta null %d%%, traded today %d",
                    summary["contracts"], summary["expiries"], summary["atm_iv_30d"],
                    summary["iv_median"], summary["iv_null_pct"], summary["delta_null_pct"],
                    summary["traded_today"],
                )
            if options.empty:
                log.warning("option chain is empty — fine outside market hours, "
                            "but stage 2 cannot cross-check IV")

        # ---- macro (optional) --------------------------------------------
        if cfg.fetch_macro:
            macro = provider.macro_series(cfg.cpi_series, cfg.yield_series)
            macro, checks = refresh_stale_series(
                macro,
                max_age_daily=cfg.macro_max_age_daily,
                max_age_monthly=cfg.macro_max_age_monthly,
                primary=provider_name,
                fetch_fallback=_macro_fallback(cfg.macro_fallback, provider_name),
                fallback_name=cfg.macro_fallback or "none",
            )
            details["macro_freshness"] = {c.series: c.as_dict() for c in checks}
            ctx.write_parquet("data_macro", macro, contracts.MACRO)
            artifacts.append("data_macro")
            details["macro_rows"] = len(macro)
            details["macro_series"] = sorted(macro["series"].dropna().unique().tolist()) if not macro.empty else []
            for label, part in macro.groupby("series"):
                last = part.sort_values("timestamp").iloc[-1]
                check = details["macro_freshness"].get(label, {})
                log.info("macro %s (%s via %s): %d rows, latest %s = %s [%s]", label,
                         last["source_symbol"], check.get("source", provider_name), len(part),
                         pd.Timestamp(last["timestamp"]).date(), round(float(last["value"]), 4),
                         check.get("action", "unchecked"))
            if macro.empty:
                log.warning("no macro rows — check data.cpi_series / data.yield_series "
                            "in config.yaml, or run: python run.py --discover-macro")

        # ---- reference values for stage 2 --------------------------------
        if cfg.fetch_reference:
            reference = collect_reference(provider, ctx.ticker)
            ctx.write_json(REFERENCE_ARTIFACT, reference)
            artifacts.append(REFERENCE_ARTIFACT)
            for name, part in reference.items():
                if "error" in part:
                    log.warning("reference %s unavailable: %s — stage 2 will not be able "
                                "to verify it", name, part["error"])

        latest = ohlcv.iloc[-1]
        return StageResult(
            stage=self.name,
            status="ok",
            summary=(f"{ctx.ticker} close {latest['close']:.2f} "
                     f"on {pd.Timestamp(latest['timestamp']).date()} via {provider_name}"),
            artifacts=artifacts,
            details=details,
        )

    def _resolve_provider(self, ctx: RunContext):
        cfg = ctx.settings.data
        try:
            return get_provider(cfg.provider, ctx.settings), cfg.provider
        except Exception as exc:  # noqa: BLE001 — any construction failure is fallback-worthy
            if not cfg.fallback_provider:
                raise
            log.warning("primary provider '%s' unavailable (%s); falling back to '%s'",
                        cfg.provider, exc, cfg.fallback_provider)
            return get_provider(cfg.fallback_provider, ctx.settings), cfg.fallback_provider


REFERENCE_ARTIFACT = "data_reference"


def collect_reference(provider, ticker: str) -> dict:
    """Market cap and next earnings, for stage 2 to cross-check.

    A failure is recorded, not raised: these values are not inputs to later
    stages, only claims to verify, and stage 2 turns a missing one into
    "unverifiable", which halts the run anyway — with the reason on record.
    """
    out: dict = {}
    for name, lookup in (("market_cap", provider.market_cap_snapshot),
                         ("next_earnings", provider.next_earnings)):
        try:
            value = lookup(ticker)
            out[name] = value if value is not None else {
                "error": f"provider '{getattr(provider, 'name', '?')}' does not offer it"}
        except Exception as exc:  # noqa: BLE001 — recorded, then enforced by stage 2
            out[name] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


def atm_iv(options: pd.DataFrame, spot: float | None, target_dte: int = 30) -> float | None:
    """IV of the at-the-money strike in the expiry nearest `target_dte`.

    The chain-wide median is dominated by short-dated, far-from-the-money
    contracts whose IVs run to hundreds of percent. A ~30-day ATM figure is the
    number people quote for a stock, so it is what settles the IV scale.
    """
    usable = options.dropna(subset=["implied_volatility", "dte", "strike"])
    if usable.empty or spot is None or pd.isna(spot):
        return None
    nearest_dte = usable.loc[(usable["dte"] - target_dte).abs().idxmin(), "dte"]
    expiry = usable[usable["dte"] == nearest_dte]
    strike = expiry.loc[(expiry["strike"] - spot).abs().idxmin(), "strike"]
    return round(float(expiry[expiry["strike"] == strike]["implied_volatility"].mean()), 4)


def _macro_fallback(name: str | None, primary: str):
    """The per-series macro fallback, or None when it would just repeat the primary."""
    if not name:
        return None
    if name != "fred":
        from ..errors import ConfigError
        raise ConfigError(f"data.macro_fallback must be 'fred' or null, got '{name}'")
    if primary == "yfinance_fred":
        return None            # the primary already is FRED
    from ..providers.yf_fred import fred_series
    return fred_series


def summarize_options(options: pd.DataFrame, spot: float | None = None) -> dict:
    """A few numbers that make a chain's health visible at a glance.

    IV figures are logged on purpose: vendors disagree on whether 45% IV is
    0.45 or 45.0, and the scale has to be settled before stage 2 compares it.
    """
    if options.empty:
        return {"contracts": 0}

    def null_pct(column: str) -> int:
        return int(round(100 * options[column].isna().mean()))

    iv = options["implied_volatility"].dropna()
    return {
        "contracts": int(len(options)),
        "expiries": int(options["expiry"].nunique()),
        "iv_median": round(float(iv.median()), 4) if not iv.empty else None,
        "atm_iv_30d": atm_iv(options, spot),
        "iv_null_pct": null_pct("implied_volatility"),
        "delta_null_pct": null_pct("delta"),
        "traded_today": int((options["volume"].fillna(0) > 0).sum()),
    }


def print_close_preview(ctx: RunContext, rows: int = 5) -> None:
    """Phase-1 acceptance aid: show recent closes so a human can eyeball them."""
    df = ctx.read_parquet("data_ohlcv", contracts.OHLCV)
    tail = df.tail(rows).copy()
    tail["date"] = pd.to_datetime(tail["timestamp"]).dt.strftime("%Y-%m-%d")
    print(f"\nLast {len(tail)} daily bars for {ctx.ticker}:")
    print(tail[["date", "open", "high", "low", "close", "volume"]].to_string(index=False))
    newest = tail.iloc[-1]
    print(f"\n  LATEST CLOSE: {newest['close']:.2f}  ({newest['date']})")
    dropped = ctx.load_manifest().get("stages", {}).get("data", {}).get("dropped_incomplete_bar")
    if dropped:
        print(f"  (session {dropped['date']} is still trading — last trade "
              f"{dropped['close']:.2f} at {dropped['market_time']} was left out)")
    print()
