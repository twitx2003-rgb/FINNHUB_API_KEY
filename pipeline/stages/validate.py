"""Stage 2 — validate.

Cross-checks what the data stage wrote against a second source and writes
`validation.json`, the file every later stage gates on.

Three checks, each ending as pass, fail or unverifiable:

- last_close  — our newest completed close vs TradingView's close for that date
- market_cap  — implied share count (cap / the price it was computed at) on both
                sides, because the LSE snapshot is not refreshed with the bars
- next_earnings — our date (Yahoo) vs TradingView's, within N days

Anything other than a pass halts the run. "Unverifiable" halts too: a check that
could not run has not passed, and the reason is written to the report.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Callable

import pandas as pd

from .. import contracts
from ..cache import RunContext
from ..errors import PipelineError, PipelineHalt
from ..gate import VALIDATION_ARTIFACT
from ..market_hours import drop_incomplete_session
from .base import Stage, StageResult
from .data import REFERENCE_ARTIFACT

log = logging.getLogger(__name__)

PASS, FAIL, UNVERIFIABLE = "pass", "fail", "unverifiable"
TV_BAR_COUNT = 10


# --------------------------------------------------------------- comparisons
def _check(name: str, status: str, detail: str, **values) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, **values}


def pct_diff(ours: float, theirs: float) -> float:
    return abs(ours - theirs) / abs(theirs) * 100.0


def check_close(ours: pd.DataFrame, theirs: pd.DataFrame, tolerance_pct: float) -> dict:
    """Newest completed close on our side vs the other source's close that day."""
    name = "last_close"
    if theirs.empty:
        return _check(name, UNVERIFIABLE, "second source returned no completed bars")

    our_by_day = ours.set_index(ours["timestamp"].dt.date)["close"]
    their_by_day = theirs.set_index(theirs["timestamp"].dt.date)["close"]
    day = our_by_day.index[-1]

    newer = [d.isoformat() for d in their_by_day.index if d > day]
    if newer:
        return _check(name, FAIL,
                      f"our data ends {day}, but the second source has completed sessions "
                      f"after it ({', '.join(newer)}) — rerun the data stage", date=day.isoformat())
    if day not in their_by_day.index:
        return _check(name, FAIL,
                      f"the second source has no completed bar for {day} "
                      f"(its latest is {their_by_day.index[-1]})", date=day.isoformat())

    o, t = float(our_by_day[day]), float(their_by_day[day])
    diff = pct_diff(o, t)
    common = our_by_day.index.intersection(their_by_day.index)
    recent = max(pct_diff(float(our_by_day[d]), float(their_by_day[d])) for d in common)
    status = PASS if diff <= tolerance_pct else FAIL
    return _check(name, status,
                  f"{day}: {o:.4f} vs {t:.4f} ({diff:.3f}% {'<=' if status == PASS else '>'} "
                  f"{tolerance_pct}%)",
                  date=day.isoformat(), ours=o, theirs=t, diff_pct=round(diff, 4),
                  tolerance_pct=tolerance_pct, overlap_days=len(common),
                  overlap_max_diff_pct=round(recent, 4))


def check_market_cap(ref: dict, theirs: dict, tolerance_pct: float) -> dict:
    """Compare implied share counts, which do not move with the price."""
    name = "market_cap"
    if ref.get("price") in (None, 0) or theirs.get("price") in (None, 0):
        return _check(name, UNVERIFIABLE,
                      "a price is missing on one side, so caps from different moments "
                      "cannot be put on a common basis")
    our_shares = ref["market_cap"] / ref["price"]
    their_shares = theirs["market_cap"] / theirs["price"]
    diff = pct_diff(our_shares, their_shares)
    status = PASS if diff <= tolerance_pct else FAIL
    return _check(name, status,
                  f"implied shares {our_shares:,.0f} vs {their_shares:,.0f} ({diff:.3f}% "
                  f"{'<=' if status == PASS else '>'} {tolerance_pct}%)",
                  ours=ref["market_cap"], ours_price=ref["price"],
                  theirs=theirs["market_cap"], theirs_price=theirs["price"],
                  diff_pct=round(diff, 4), tolerance_pct=tolerance_pct)


def check_earnings(ref: dict, theirs: dict, tolerance_days: int) -> dict:
    """Their date must fall inside our date (or window) widened by the tolerance."""
    name = "next_earnings"
    ours = [date.fromisoformat(d) for d in ref["dates"]]
    their_day = date.fromisoformat(theirs["date"])
    low, high = min(ours), max(ours)
    off = max((low - their_day).days, (their_day - high).days, 0)
    status = PASS if off <= tolerance_days else FAIL
    window = low.isoformat() if low == high else f"{low}..{high}"
    return _check(name, status,
                  f"{window} vs {their_day} ({off} day(s) apart, tolerance {tolerance_days})",
                  ours=[d.isoformat() for d in ours], theirs=their_day.isoformat(),
                  days_apart=off, tolerance_days=tolerance_days)


# ------------------------------------------------------------------- sources
class SyntheticSource:
    """Offline second source for --selftest and tests: agrees with the cache."""

    def __init__(self, ctx: RunContext):
        self.ctx = ctx

    def daily_bars(self, symbol: str, count: int = TV_BAR_COUNT) -> pd.DataFrame:
        return self.ctx.read_parquet("data_ohlcv").tail(count).reset_index(drop=True)

    def market_cap(self, symbol: str) -> dict:
        ref = self.ctx.read_json(REFERENCE_ARTIFACT)["market_cap"]
        return {"market_cap": ref["market_cap"], "price": ref["price"]}

    def next_earnings(self, symbol: str) -> dict:
        return {"date": self.ctx.read_json(REFERENCE_ARTIFACT)["next_earnings"]["dates"][0]}


def default_source(ctx: RunContext):
    v = ctx.settings.validate
    if v.provider == "synthetic":
        return SyntheticSource(ctx), "synthetic"

    from ..providers.tradingview_data import TradingViewData
    from ..providers.tradingview_mcp import TradingViewMCP

    client = TradingViewMCP(url=v.tradingview_url, token_path=v.tradingview_token_path,
                            callback_host=v.tradingview_callback_host,
                            callback_port=v.tradingview_callback_port, interactive=False)
    dump_dir = ctx.settings.log_dir / "tradingview_payloads"
    return TradingViewData(client, delays=v.rate_limit_delays, dump_dir=dump_dir), "tradingview"


# --------------------------------------------------------------------- stage
class ValidateStage(Stage):
    name = "validate"
    requires_validation = False      # this stage produces the gate file

    def __init__(self, source_factory: Callable[[RunContext], tuple[Any, str]] = default_source):
        self.source_factory = source_factory

    def run(self, ctx: RunContext) -> StageResult:
        if not ctx.exists("data_ohlcv"):
            raise PipelineHalt(f"no data for {ctx.ticker} {ctx.run_date} — run the data stage first")

        v, d = ctx.settings.validate, ctx.settings.data
        ours = ctx.read_parquet("data_ohlcv", contracts.OHLCV)
        reference = (ctx.read_json(REFERENCE_ARTIFACT)
                     if ctx.exists(REFERENCE_ARTIFACT, ".json") else {})
        symbol = v.tradingview_symbol(ctx.ticker)
        source, source_name = self.source_factory(ctx)

        def close_check():
            theirs = source.daily_bars(symbol, TV_BAR_COUNT)
            if d.drop_incomplete_session:
                theirs, _ = drop_incomplete_session(theirs, market_tz=d.market_timezone,
                                                    session_close=d.session_close)
            return check_close(ours, theirs, v.close_tolerance_pct)

        def cap_check():
            ref = reference.get("market_cap") or {"error": "data_reference.json missing"}
            if "error" in ref:
                return _check("market_cap", UNVERIFIABLE, f"no reference value: {ref['error']}")
            return check_market_cap(ref, source.market_cap(symbol), v.market_cap_tolerance_pct)

        def earnings_check():
            ref = reference.get("next_earnings") or {"error": "data_reference.json missing"}
            if "error" in ref:
                return _check("next_earnings", UNVERIFIABLE, f"no reference value: {ref['error']}")
            return check_earnings(ref, source.next_earnings(symbol), v.earnings_tolerance_days)

        checks = [_run_check(name, fn) for name, fn in (
            ("last_close", close_check), ("market_cap", cap_check),
            ("next_earnings", earnings_check))]

        status = PASS if all(c["status"] == PASS for c in checks) else FAIL
        ctx.write_json(VALIDATION_ARTIFACT, {
            "status": status, "ticker": ctx.ticker, "symbol": symbol,
            "run_date": ctx.run_date, "source": source_name,
            "checked_at": datetime.now(timezone.utc).isoformat(), "checks": checks,
        })
        for c in checks:
            level = logging.INFO if c["status"] == PASS else logging.WARNING
            log.log(level, "check %-13s %-12s %s", c["name"], c["status"].upper(), c["detail"])

        if status != PASS:
            bad = ", ".join(f"{c['name']}={c['status']}" for c in checks if c["status"] != PASS)
            raise PipelineHalt(f"validation against {source_name} did not pass ({bad}). "
                               f"Details: {ctx.path(VALIDATION_ARTIFACT, '.json')}")
        return StageResult(stage=self.name, status="ok",
                           summary=f"{len(checks)} checks passed against {source_name}",
                           artifacts=[VALIDATION_ARTIFACT])


def _run_check(name: str, fn: Callable[[], dict]) -> dict:
    """A source failure makes the check unverifiable instead of crashing the stage."""
    try:
        return fn()
    except PipelineError as exc:   # ProviderError, RateLimited, AuthorizationRequired...
        return _check(name, UNVERIFIABLE, f"{type(exc).__name__}: {exc}")
