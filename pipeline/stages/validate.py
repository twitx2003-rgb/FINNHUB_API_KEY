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

One exception, chosen by the user: when the earnings dates disagree and at least
one of them is visibly an estimate (a weekend date, or a date window), the check
ends as "warn". The run continues, the date is recorded as unconfirmed, and later
stages must not present it as fact. Two firm dates that disagree still fail.
"""
from __future__ import annotations

import json
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

PASS, FAIL, UNVERIFIABLE, WARN = "pass", "fail", "unverifiable", "warn"
OK_STATUSES = (PASS, WARN)
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
    # Every overlapping day, not just the checked one: tells a one-day effect
    # (e.g. a bar still moving after hours) apart from a systematic difference.
    overlap = [{"date": d.isoformat(), "ours": float(our_by_day[d]),
                "theirs": float(their_by_day[d]),
                "diff_pct": round(pct_diff(float(our_by_day[d]), float(their_by_day[d])), 4)}
               for d in common]
    recent = max(row["diff_pct"] for row in overlap)
    status = PASS if diff <= tolerance_pct else FAIL
    return _check(name, status,
                  f"{day}: {o:.4f} vs {t:.4f} ({diff:.3f}% {'<=' if status == PASS else '>'} "
                  f"{tolerance_pct}%)",
                  date=day.isoformat(), ours=o, theirs=t, diff_pct=round(diff, 4),
                  tolerance_pct=tolerance_pct, overlap_days=len(common),
                  overlap_max_diff_pct=recent, overlap=overlap)


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
    """Their date must fall inside our date (or window) widened by the tolerance.

    Disagreement where either side is visibly an estimate is a warning, not a
    failure: the date is then marked unconfirmed (`confirmed: False`).
    """
    name = "next_earnings"
    ours = [date.fromisoformat(d) for d in ref["dates"]]
    their_day = date.fromisoformat(theirs["date"])
    low, high = min(ours), max(ours)
    off = max((low - their_day).days, (their_day - high).days, 0)
    window = low.isoformat() if low == high else f"{low}..{high}"

    # Companies report on trading days, so a weekend date is a placeholder; a
    # window (Yahoo gives two dates when unconfirmed) is an estimate by definition.
    reasons = [f"{who} date {d} is a {d:%A}" for who, d in
               (("our", low), ("our", high), ("their", their_day)) if d.weekday() >= 5]
    if low != high:
        reasons.append(f"our date is a window ({window})")
    reasons = list(dict.fromkeys(reasons))
    estimated = bool(reasons)

    if off <= tolerance_days:
        status, confirmed = PASS, not estimated
    elif estimated:
        status, confirmed = WARN, False
    else:
        status, confirmed = FAIL, False
    note = f" — {'; '.join(reasons)}, so likely an estimate" if reasons else ""
    if status == WARN:
        note += "; the date is recorded as unconfirmed"
    return _check(name, status,
                  f"{window} vs {their_day} ({off} day(s) apart, tolerance {tolerance_days}){note}",
                  ours=[d.isoformat() for d in ours], theirs=their_day.isoformat(),
                  days_apart=off, tolerance_days=tolerance_days, estimated=estimated,
                  confirmed=confirmed)


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


# ---------------------------------------------------------- saved answers
SAVED_ANSWERS = "tradingview_reference.json"


class SavedAnswers:
    """TradingView's last successful market-cap and earnings answers, per symbol.

    TradingView's scanner (behind both tools) answers 429 for hours at a time,
    while the values themselves move slowly: the share count changes quarterly
    and an earnings date rarely moves. User decision (2026-09-23): when the live
    call is rate limited, a saved answer no older than its limit stands in, and
    the check says where it came from. Only a rate limit falls back — any other
    failure still surfaces. The closing price is always checked live.

    Kept per ticker, next to the run folders: cache/<TICKER>/tradingview_reference.json
    """

    def __init__(self, path):
        self.path = path

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def put(self, symbol: str, kind: str, value: dict, now: datetime) -> None:
        data = self._read()
        data.setdefault(symbol, {})[kind] = {"value": value, "fetched_at": now.isoformat()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def get(self, symbol: str, kind: str, max_age_days: float, now: datetime) -> dict | None:
        entry = self._read().get(symbol, {}).get(kind)
        if not entry:
            return None
        fetched = datetime.fromisoformat(entry["fetched_at"])
        if (now - fetched).total_seconds() > max_age_days * 86400:
            return None
        return entry


def fetch_or_saved(fetch: Callable[[], dict], saved: SavedAnswers, symbol: str, kind: str,
                   max_age_days: float, now: datetime | None = None) -> tuple[dict, str | None]:
    """The live answer (saved for next time), or — only when rate limited — a
    recent saved one. Returns (value, fetched_at of the saved answer or None)."""
    from ..providers.tradingview_data import RateLimited

    now = now or datetime.now(timezone.utc)
    try:
        value = fetch()
    except RateLimited as exc:
        entry = saved.get(symbol, kind, max_age_days, now)
        if entry is None:
            raise RateLimited(f"{exc}; no saved answer from the last {max_age_days:g} day(s) "
                              "to fall back on") from None
        log.warning("%s: TradingView rate limited; using its answer saved at %s", kind,
                    entry["fetched_at"])
        return entry["value"], entry["fetched_at"]
    saved.put(symbol, kind, value, now)
    return value, None


def _mark_saved(check: dict, fetched_at: str | None) -> dict:
    if fetched_at:
        check["theirs_saved_at"] = fetched_at
        check["detail"] += f" [TradingView answer saved {fetched_at[:16].replace('T', ' ')} UTC; live call rate limited]"
    return check


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
        saved = SavedAnswers(ctx.run_dir.parent / SAVED_ANSWERS)

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
            theirs, saved_at = fetch_or_saved(lambda: source.market_cap(symbol), saved, symbol,
                                              "market_cap", v.saved_market_cap_max_age_days)
            return _mark_saved(check_market_cap(ref, theirs, v.market_cap_tolerance_pct), saved_at)

        def earnings_check():
            ref = reference.get("next_earnings") or {"error": "data_reference.json missing"}
            if "error" in ref:
                return _check("next_earnings", UNVERIFIABLE, f"no reference value: {ref['error']}")
            theirs, saved_at = fetch_or_saved(lambda: source.next_earnings(symbol), saved, symbol,
                                              "next_earnings", v.saved_earnings_max_age_days)
            return _mark_saved(check_earnings(ref, theirs, v.earnings_tolerance_days), saved_at)

        checks = [_run_check(name, fn) for name, fn in (
            ("last_close", close_check), ("market_cap", cap_check),
            ("next_earnings", earnings_check))]

        status = PASS if all(c["status"] in OK_STATUSES for c in checks) else FAIL
        ctx.write_json(VALIDATION_ARTIFACT, {
            "status": status, "ticker": ctx.ticker, "symbol": symbol,
            "run_date": ctx.run_date, "source": source_name,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "warnings": [c["name"] for c in checks if c["status"] == WARN],
            "checks": checks,
        })
        for c in checks:
            level = logging.INFO if c["status"] == PASS else logging.WARNING
            log.log(level, "check %-13s %-12s %s", c["name"], c["status"].upper(), c["detail"])

        if status != PASS:
            bad = ", ".join(f"{c['name']}={c['status']}" for c in checks
                            if c["status"] not in OK_STATUSES)
            raise PipelineHalt(f"validation against {source_name} did not pass ({bad}). "
                               f"Details: {ctx.path(VALIDATION_ARTIFACT, '.json')}")
        warned = [c["name"] for c in checks if c["status"] == WARN]
        summary = f"{len(checks)} checks passed against {source_name}"
        if warned:
            summary += f" ({', '.join(warned)} with a warning — see validation.json)"
        return StageResult(stage=self.name, status="ok", summary=summary,
                           artifacts=[VALIDATION_ARTIFACT])


def _run_check(name: str, fn: Callable[[], dict]) -> dict:
    """A source failure makes the check unverifiable instead of crashing the stage."""
    try:
        return fn()
    except PipelineError as exc:   # ProviderError, RateLimited, AuthorizationRequired...
        return _check(name, UNVERIFIABLE, f"{type(exc).__name__}: {exc}")
