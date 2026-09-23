"""The debate brief: every fact the debaters may use, built only from validated
artifacts in the run directory.

Each fact has a stable key, a value, a unit, a date and its source. Debaters
must cite fact keys for every claim, and pipeline.debate drops arguments whose
citations are not keys of this brief — so the brief is the whole universe of
data that reaches the model. Nothing here fetches anything.

Required: data_ohlcv (and a passing validation.json — the stage gate checks it).
Every other artifact is optional; a missing one is listed under `missing` so the
debaters know what they were not shown.
"""
from __future__ import annotations

import logging
import math

import numpy as np
import pandas as pd

from . import contracts
from .cache import RunContext
from .errors import ProviderError

log = logging.getLogger(__name__)

TRADING_DAYS = 252
ATM_BAND = 0.025                 # strikes within ±2.5% of the underlying
ATM_DTE = (20, 45)               # "about a month" of expiry


class Brief:
    def __init__(self, ticker: str, run_date: str):
        self.ticker = ticker
        self.run_date = run_date
        self.facts: dict[str, dict] = {}
        self.missing: list[str] = []
        self.caveats: list[str] = []

    def add(self, key: str, value, *, unit: str, as_of: str | None, source: str,
            note: str | None = None) -> None:
        if key in self.facts:
            raise ProviderError(f"brief: fact '{key}' added twice")
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ProviderError(f"brief: fact '{key}' is not a finite number")
            value = round(value, 4)
        fact = {"value": value, "unit": unit, "as_of": as_of, "source": source}
        if note:
            fact["note"] = note
        self.facts[key] = fact

    def to_dict(self) -> dict:
        return {"ticker": self.ticker, "run_date": self.run_date, "facts": self.facts,
                "missing": self.missing, "caveats": self.caveats}


def _day(ts) -> str:
    return pd.Timestamp(ts).date().isoformat()


def _optional_json(ctx: RunContext, name: str, brief: Brief) -> dict | None:
    if not ctx.exists(name, ".json"):
        brief.missing.append(name)
        return None
    return ctx.read_json(name)


# ---------------------------------------------------------------- sections
def add_price(brief: Brief, ohlcv: pd.DataFrame) -> None:
    bars = ohlcv.sort_values("timestamp").reset_index(drop=True)
    close = bars["close"].astype(float)
    last_day = _day(bars["timestamp"].iloc[-1])
    src = "LSE regular-session daily bars; latest close cross-checked against TradingView"
    brief.add("price.last_close", float(close.iloc[-1]), unit="USD", as_of=last_day, source=src)
    for n in (5, 20, 60, 250):
        if len(close) > n:
            brief.add(f"price.change_{n}d_pct", 100 * (close.iloc[-1] / close.iloc[-1 - n] - 1),
                      unit="%", as_of=last_day, source=src, note=f"change over the last {n} sessions")
    year = bars.iloc[-TRADING_DAYS:]
    high, low = float(year["high"].max()), float(year["low"].min())
    brief.add("price.high_52w", high, unit="USD", as_of=last_day, source=src,
              note=f"highest high of the last {len(year)} sessions")
    brief.add("price.low_52w", low, unit="USD", as_of=last_day, source=src,
              note=f"lowest low of the last {len(year)} sessions")
    brief.add("price.pct_below_52w_high", 100 * (1 - close.iloc[-1] / high), unit="%",
              as_of=last_day, source=src)
    if len(close) > 21:
        rets = np.diff(np.log(close.iloc[-21:].to_numpy()))
        brief.add("volatility.realized_20d_pct", 100 * float(np.std(rets, ddof=1)) * math.sqrt(TRADING_DAYS),
                  unit="% annualised", as_of=last_day, source=src,
                  note="standard deviation of the last 20 daily log returns, x sqrt(252)")
    volume = bars["volume"].astype(float)
    if len(volume) >= 20:
        brief.add("volume.avg_20d", float(volume.iloc[-20:].mean()), unit="shares", as_of=last_day,
                  source=src, note="regular session only")
        brief.add("volume.last_vs_avg_20d", float(volume.iloc[-1] / volume.iloc[-20:].mean()),
                  unit="ratio", as_of=last_day, source=src)


def add_options(brief: Brief, options: pd.DataFrame) -> None:
    src = "LSE option chain (lists only contracts that traded that day)"
    if options.empty:
        brief.caveats.append("The option chain was empty for this run.")
        return
    as_of = str(options["updated_at"].dropna().max())[:10] or None
    near = options[(options["dte"] >= ATM_DTE[0]) & (options["dte"] <= ATM_DTE[1])
                   & options["implied_volatility"].notna() & options["underlying_price"].notna()]
    near = near[(near["strike"] / near["underlying_price"] - 1).abs() <= ATM_BAND]
    if len(near):
        iv = float(near["implied_volatility"].median())
        if iv > 5:
            raise ProviderError(f"options: median IV {iv:.3g} looks like percent, not a fraction — "
                                "the vendor's IV scale changed; not guessing")
        brief.add("options.atm_iv_30d_pct", 100 * iv, unit="% annualised", as_of=as_of, source=src,
                  note=f"median IV of {len(near)} contracts, {ATM_DTE[0]}-{ATM_DTE[1]} days to "
                       f"expiry, strike within ±{ATM_BAND:.1%} of the underlying")
    else:
        brief.caveats.append("No near-the-money ~30-day contracts with IV: no implied volatility fact.")
    calls = float(options.loc[options["type"] == "call", "volume"].sum())
    puts = float(options.loc[options["type"] == "put", "volume"].sum())
    if calls > 0:
        brief.add("options.put_call_volume_ratio", puts / calls, unit="ratio", as_of=as_of, source=src,
                  note=f"all listed expiries up to {int(options['dte'].max())} days")


def add_macro(brief: Brief, macro: pd.DataFrame) -> None:
    for series, key, unit in (("CPI", "macro.cpi_yoy_pct", "%"), ("YIELD_10Y", "macro.us10y_pct", "%")):
        s = macro[macro["series"] == series].sort_values("timestamp")
        if s.empty:
            brief.caveats.append(f"Macro series {series} is missing.")
            continue
        last = s.iloc[-1]
        src = f"{last['source_symbol']} (macro, freshness-checked)"
        if series == "CPI":
            year_ago = s[s["timestamp"] == last["timestamp"] - pd.DateOffset(years=1)]
            if year_ago.empty:
                brief.caveats.append("CPI has no value exactly 12 months before its latest: no YoY fact.")
                continue
            brief.add(key, 100 * (last["value"] / year_ago["value"].iloc[0] - 1), unit=unit,
                      as_of=_day(last["timestamp"]), source=src, note="CPI month-on-same-month-last-year")
        else:
            brief.add(key, float(last["value"]), unit=unit, as_of=_day(last["timestamp"]), source=src)
            if len(s) > 20:
                brief.add("macro.us10y_change_20obs_pct_pts", float(last["value"] - s["value"].iloc[-21]),
                          unit="percentage points", as_of=_day(last["timestamp"]), source=src)


def add_reference(brief: Brief, reference: dict | None, validation: dict) -> None:
    checks = {c.get("name"): c for c in validation.get("checks", [])}
    if reference and isinstance(reference.get("market_cap"), dict) and "market_cap" in reference["market_cap"]:
        mc = reference["market_cap"]
        brief.add("company.market_cap", float(mc["market_cap"]), unit="USD",
                  as_of=str(mc.get("as_of", ""))[:10] or None, source=f"{mc.get('source')}, "
                  f"validation: {checks.get('market_cap', {}).get('status', 'not checked')}")
    earnings = checks.get("next_earnings")
    if reference and isinstance(reference.get("next_earnings"), dict) and reference["next_earnings"].get("dates"):
        dates = reference["next_earnings"]["dates"]
        confirmed = bool(earnings and earnings.get("status") == "pass")
        if earnings and "confirmed" in earnings:
            confirmed = bool(earnings["confirmed"])
        brief.add("company.next_earnings_date", dates if len(dates) > 1 else dates[0], unit="date",
                  as_of=brief.run_date, source=f"{reference['next_earnings'].get('source')}, "
                  f"validation: {earnings.get('status') if earnings else 'not checked'}",
                  note=None if confirmed else "NOT CONFIRMED — the sources disagree or one is an "
                                              "estimate; must not be stated as a fact")
        brief.add("company.next_earnings_confirmed", confirmed, unit="bool", as_of=brief.run_date,
                  source="validation")
    brief.add("validation.status", validation.get("status"), unit="text", as_of=brief.run_date,
              source="stage 2 cross-check against TradingView")
    for w in validation.get("warnings", []) or []:
        brief.caveats.append(f"Validation warning: {w}")


def add_insiders(brief: Brief, insider: dict | None, holders: dict | None) -> None:
    if insider:
        s = insider["summary"]
        src = f"SEC EDGAR Form 4, {insider['filings_read']} filings since {insider['since']}"
        buys, sales = s["open_market_buys"], s["open_market_sales"]
        brief.add("insiders.open_market_buys_count", buys["count"], unit="transactions", as_of=brief.run_date, source=src)
        brief.add("insiders.open_market_buys_usd", float(buys["value_usd"]), unit="USD", as_of=brief.run_date, source=src)
        brief.add("insiders.open_market_sales_count", sales["count"], unit="transactions", as_of=brief.run_date, source=src)
        brief.add("insiders.open_market_sales_usd", float(sales["value_usd"]), unit="USD", as_of=brief.run_date, source=src)
        brief.add("insiders.sales_under_10b5_1_pct", s["sale_shares_under_10b5_1_pct"], unit="% of sold shares",
                  as_of=brief.run_date, source=src,
                  note="share of sold shares whose Form 4 marks a pre-arranged 10b5-1 plan, as filed")
        brief.add("insiders.distinct_insiders", s["insiders"], unit="people", as_of=brief.run_date, source=src)
    if holders:
        src = f"SEC EDGAR Schedule 13G/13D since {holders['since']} (holders >5% only)"
        brief.add("holders.over_5pct", [{"holder": h["holder"], "percent": h["percent"], "filed": h["filed"]}
                                        for h in holders.get("holders", [])],
                  unit="list", as_of=brief.run_date, source=src,
                  note="only holders that filed a machine-readable Schedule 13; holders below 5% file none")


def add_forecasts(brief: Brief, timesfm: dict | None, kronos: dict | None) -> None:
    if timesfm and "volume" in timesfm.get("series", {}):
        v = timesfm["series"]["volume"]
        bt = v["backtest"]
        brief.add("forecast.volume_backtest", {"error_pct": bt["mape_pct"], "naive_error_pct": bt["naive_mape_pct"],
                                               "band_held_pct": bt["coverage_pct"],
                                               "band_target_pct": bt["coverage_target_pct"]},
                  unit="%", as_of=v["last_date"], source=f"TimesFM ({timesfm['model']})",
                  note=f"{bt['windows']} windows x {bt['horizon']} sessions")
    if kronos:
        final = kronos["final_close_change_pct"]
        brief.add("scenarios.close_change_range_pct", final, unit="% vs last close", as_of=kronos["last_date"],
                  source=f"Kronos ({kronos['model']}), {kronos['paths']} sampled paths",
                  note=f"SCENARIO RANGE after {kronos['horizon']} sessions ({kronos['interval'][0]:.0%}-"
                       f"{kronos['interval'][1]:.0%} of paths), NOT a prediction or a price target")
        brief.add("scenarios.share_of_paths_up_pct", kronos["share_of_paths_up_pct"], unit="%",
                  as_of=kronos["last_date"], source="Kronos",
                  note="share of sampled paths ending above the last close — not a probability estimate")
        brief.add("scenarios.degraded", kronos["degraded"], unit="bool", as_of=kronos["last_date"], source="Kronos",
                  note=f"{kronos['invalid_pct']}% of generated candles were impossible bars and were dropped")
        brief.caveats.extend(f"Kronos: {c}" for c in kronos.get("caveats", []))


def build_brief(ctx: RunContext, validation: dict) -> Brief:
    brief = Brief(ctx.ticker, ctx.run_date)
    add_price(brief, ctx.read_parquet("data_ohlcv", contracts.OHLCV))
    if ctx.exists("data_options"):
        add_options(brief, ctx.read_parquet("data_options", contracts.OPTIONS_CHAIN))
    else:
        brief.missing.append("data_options")
    if ctx.exists("data_macro"):
        add_macro(brief, ctx.read_parquet("data_macro", contracts.MACRO))
    else:
        brief.missing.append("data_macro")
    add_reference(brief, _optional_json(ctx, "data_reference", brief), validation)
    add_insiders(brief, _optional_json(ctx, "extract_insider", brief),
                 _optional_json(ctx, "extract_holders", brief))
    add_forecasts(brief, _optional_json(ctx, "forecast_timesfm", brief),
                  _optional_json(ctx, "forecast_kronos", brief))
    brief.caveats.append("No news, financial statements or analyst estimates are in this brief.")
    return brief
