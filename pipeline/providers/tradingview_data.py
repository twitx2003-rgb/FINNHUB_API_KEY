"""TradingView MCP tool results -> checked Python data.

`tradingview_mcp.py` is the transport (OAuth, sessions). This module turns tool
results into data the validate stage can trust, and is strict about it because
the server is a public beta whose output formats are undocumented.

What the live server showed (first calls, 2026-09):

- A failed call is NOT flagged at the MCP level: `is_error` is False and the
  payload is `{"success": false, "error": "..."}`. Reading `is_error` alone
  would pass a failure off as data.
- The screener-backed tools (symbol data, earnings calendar) at first returned
  `tradingview api: https://scanner.tradingview.com/...: 429` for hours — a rate
  limit behind the MCP server. It is retried with backoff; anything else fails
  at once. Their success shapes were mapped once the scanner answered.
- `get-ohlcv` returns bars `{t, o, h, l, c, v}` oldest first, `t` in unix
  seconds at the session open (13:30 UTC for NASDAQ), and the newest bar can be
  today's unfinished session.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ..contracts import assert_ohlcv_sane
from ..errors import ProviderError
from .base import pick

log = logging.getLogger(__name__)

OHLCV_TOOL = "mcp-tv-get-ohlcv"
SYMBOL_DATA_TOOL = "mcp-tv-get-symbol-data"
EARNINGS_TOOL = "mcp-tv-get-earnings-calendar"
MARKET_CAP_COLUMNS = ["close", "market_cap_basic"]
RATE_LIMIT_DELAYS = (5.0, 15.0, 45.0)


class ToolFailed(ProviderError):
    """The tool ran and reported failure in its own payload."""


class RateLimited(ToolFailed):
    """The failure is a 429 from TradingView's backend; worth retrying."""


class ShapeNotMapped(ProviderError):
    """The tool answered, but not in the format mapped here (a public beta can
    change it). Raised instead of guessing; the payload is saved for a look."""


def tool_payload(result: Any, tool: str) -> dict[str, Any]:
    """The JSON object a tool returned, or a typed failure.

    Prefers `structured_content`; falls back to the first text block when that
    is JSON. Refuses anything that is not an object, and any payload that says
    `success: false`.
    """
    if result.is_error:
        raise ToolFailed(f"{tool}: tool error: {_text(result)[:300]}")

    payload = result.structured_content
    if payload is None:
        try:
            payload = json.loads(_text(result))
        except ValueError:
            raise ProviderError(f"{tool}: result is not JSON: {_text(result)[:300]!r}") from None
    if not isinstance(payload, dict):
        raise ProviderError(f"{tool}: expected a JSON object, got {type(payload).__name__}")

    if payload.get("success") is False:
        error = str(payload.get("error") or "(no error text)")
        kind = RateLimited if re.search(r"(?<!\d)429(?!\d)", error) else ToolFailed
        raise kind(f"{tool}: {error}")
    return payload


def _text(result: Any) -> str:
    return "\n".join(t for t in (getattr(b, "text", None) for b in result.content) if t)


def bars_frame(payload: dict[str, Any], symbol: str) -> pd.DataFrame:
    """`get-ohlcv` payload -> frame in the data_ohlcv column layout, oldest first."""
    context = f"{OHLCV_TOOL} {symbol}"
    returned = payload.get("symbol")
    if returned is not None and returned != symbol:
        raise ProviderError(f"{context}: asked for {symbol}, server answered for {returned}")

    bars = pick(payload, ["bars"], context=context)
    if not isinstance(bars, list):
        raise ProviderError(f"{context}: 'bars' is {type(bars).__name__}, expected a list")
    if not bars:
        raise ProviderError(f"{context}: no bars returned")

    rows = [{
        "timestamp": pd.Timestamp(pick(b, ["t"], context=context), unit="s", tz="UTC"),
        "symbol": symbol,
        "open": float(pick(b, ["o"], context=context)),
        "high": float(pick(b, ["h"], context=context)),
        "low": float(pick(b, ["l"], context=context)),
        "close": float(pick(b, ["c"], context=context)),
        "volume": float(pick(b, ["v"], context=context)),
    } for b in bars]
    df = pd.DataFrame(rows)

    # Oldest first is what the live server sends; verify instead of assuming.
    if not df["timestamp"].is_monotonic_increasing:
        raise ProviderError(f"{context}: bars are not in ascending time order")
    dates = df["timestamp"].dt.date
    if dates.duplicated().any():
        raise ProviderError(f"{context}: more than one bar for {dates[dates.duplicated()].iloc[0]}")
    return assert_ohlcv_sane(df)


def _data_object(payload: dict[str, Any], context: str) -> dict[str, Any]:
    data = pick(payload, ["data"], context=context)
    if not isinstance(data, dict):
        raise ProviderError(f"{context}: 'data' is {type(data).__name__}, expected an object")
    return data


def market_cap_from_payload(payload: dict[str, Any], symbol: str) -> dict[str, Any]:
    """`get-symbol-data` with columns [close, market_cap_basic].

    Live shape: {"success": true, "data": {"close": ..., "market_cap_basic": ...}}
    """
    context = f"{SYMBOL_DATA_TOOL} {symbol}"
    data = _data_object(payload, context)
    cap = float(pick(data, ["market_cap_basic"], context=context))
    price = float(pick(data, ["close"], context=context))
    if cap <= 0 or price <= 0:
        raise ProviderError(f"{context}: non-positive market cap ({cap}) or close ({price})")
    return {"market_cap": cap, "price": price}


def earnings_from_payload(payload: dict[str, Any], symbol: str) -> dict[str, Any]:
    """`get-earnings-calendar` for one symbol.

    Live shape: {"success": true, "data": {"count", "from", "to", "earnings": [
    {"symbol", "name", "release_date", "release_next_date", ...}]}}
    """
    context = f"{EARNINGS_TOOL} {symbol}"
    data = _data_object(payload, context)
    rows = pick(data, ["earnings"], context=context)
    if not isinstance(rows, list):
        raise ProviderError(f"{context}: 'earnings' is {type(rows).__name__}, expected a list")
    mine = [r for r in rows if isinstance(r, dict) and r.get("symbol") == symbol]
    if len(mine) != 1:
        raise ProviderError(f"{context}: expected one row for {symbol}, got {len(mine)} "
                            f"(symbols returned: {[r.get('symbol') for r in rows if isinstance(r, dict)]})")
    row = mine[0]
    upcoming = pick(row, ["release_next_date"], context=context, allow_null=True)
    if not upcoming:
        raise ProviderError(f"{context}: no upcoming earnings date")
    last = pick(row, ["release_date"], context=context, allow_null=True)
    return {"date": pd.Timestamp(upcoming).date().isoformat(),
            "last": pd.Timestamp(last).date().isoformat() if last else None}


class TradingViewData:
    """Read-only data calls on top of a TradingViewMCP client."""

    def __init__(self, client: Any, *, delays: tuple[float, ...] = RATE_LIMIT_DELAYS,
                 sleep: Callable[[float], None] = time.sleep, dump_dir: Path | None = None):
        self.client = client
        self.delays = delays
        self.sleep = sleep
        self.dump_dir = dump_dir

    def fetch(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool and return its checked payload, retrying only on 429."""
        for delay in (*self.delays, None):
            try:
                return tool_payload(self.client.call_tool(tool, arguments), tool)
            except RateLimited as exc:
                if delay is None:
                    raise RateLimited(f"{exc} (still rate limited after "
                                      f"{len(self.delays) + 1} attempts)") from None
                log.warning("%s rate limited by TradingView; retrying in %.0fs", tool, delay)
                self.sleep(delay)
        raise AssertionError("unreachable")

    def daily_bars(self, symbol: str, count: int = 10) -> pd.DataFrame:
        payload = self.fetch(OHLCV_TOOL, {"symbol": symbol, "interval": "1D", "count": count})
        return bars_frame(payload, symbol)

    def market_cap(self, symbol: str) -> dict[str, Any]:
        """{"market_cap", "price"}: the cap and the close it goes with, from one response."""
        payload = self.fetch(SYMBOL_DATA_TOOL, {"symbol": symbol, "columns": MARKET_CAP_COLUMNS})
        return self._parse(SYMBOL_DATA_TOOL, payload, market_cap_from_payload, symbol)

    def next_earnings(self, symbol: str) -> dict[str, Any]:
        """{"date", "last"}: the next scheduled earnings date and the previous one."""
        payload = self.fetch(EARNINGS_TOOL, {"symbols": [symbol]})
        return self._parse(EARNINGS_TOOL, payload, earnings_from_payload, symbol)

    def _parse(self, tool: str, payload: dict[str, Any], parser, symbol: str) -> dict[str, Any]:
        try:
            return parser(payload, symbol)
        except ProviderError as exc:
            where = "not saved"
            if self.dump_dir is not None:
                self.dump_dir.mkdir(parents=True, exist_ok=True)
                target = self.dump_dir / f"{tool}.json"
                target.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
                where = str(target)
            raise ShapeNotMapped(f"{exc}. Response saved to {where}; not guessing field "
                                 "names.") from None
