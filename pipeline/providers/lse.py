"""lse-data provider.

Verified against lse-data 0.14.0 by introspection, not documentation:

    from lse import LSE, LSEError
    LSE(api_key=None, url=..., timeout=60)          # reads LSE_API_KEY if omitted
    candles(symbol, timeframe="1m", start, end, limit<=5000, order="asc", dataset)
    options(underlying, type, expiry, strike, min_dte, max_dte, limit<=5000)
    economics(symbol, start, end, order="asc", limit)   -> series(dataset="economics")
    bond_yields(symbol, start, end, order="asc", limit)

THE TRAP: `order` defaults to "asc" on candles/economics/bond_yields, so the
default response starts at the *beginning* of history (stocks go back to 2003).
Confusingly, dividends/splits/insider_trades/options_flow default to "desc"
instead — so there is no single default to remember. We therefore always pass
order explicitly and verify the response really is descending.
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from ..contracts import OPTIONS_COLUMNS, normalize_options
from ..errors import ProviderError
from .base import (
    MarketDataProvider,
    assert_descending,
    pick,
    pick_optional,
    window_start,
)

log = logging.getLogger(__name__)

_MAX_ROWS = 5000  # lse-data clamps limit server-side with min(int(limit), 5000)


class LSEProvider(MarketDataProvider):
    name = "lse"

    def __init__(self, api_key: str | None = None, timeout: float = 60.0):
        try:
            from lse import LSE, LSEError  # noqa: N811
        except ImportError as exc:  # pragma: no cover
            raise ProviderError("lse-data is not installed — pip install lse-data") from exc
        self._LSEError = LSEError
        self._client = LSE(api_key=api_key, timeout=timeout)

    def _call(self, method: str, /, **kwargs) -> list[dict[str, Any]]:
        try:
            rows = getattr(self._client, method)(**kwargs)
        except self._LSEError as exc:
            raise ProviderError(f"lse-data {method}() failed: {exc}") from exc
        if not isinstance(rows, list):
            raise ProviderError(f"lse-data {method}() returned {type(rows).__name__}, expected list")
        return rows

    # ------------------------------------------------------------------ OHLCV
    def daily_ohlcv(self, symbol: str, lookback_days: int) -> pd.DataFrame:
        rows = self._call(
            "candles",
            symbol=symbol,
            timeframe="1d",
            start=window_start(lookback_days),
            limit=_MAX_ROWS,
            order="desc",  # never rely on the default — see module docstring
        )
        if not rows:
            raise ProviderError(f"lse-data candles(): no daily bars for '{symbol}'")

        assert_descending(rows, "timestamp", context=f"lse candles({symbol})")

        frame = pd.DataFrame(
            {
                "timestamp": [pick(r, ("timestamp", "ts"), context="lse candles") for r in rows],
                "symbol": symbol,
                "open": [pick(r, ("open", "o"), context="lse candles") for r in rows],
                "high": [pick(r, ("high", "h"), context="lse candles") for r in rows],
                "low": [pick(r, ("low", "l"), context="lse candles") for r in rows],
                "close": [pick(r, ("close", "c"), context="lse candles") for r in rows],
                "volume": [pick_optional(r, ("volume", "v"), 0.0) for r in rows],
            }
        )
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, format="mixed")
        for column in ("open", "high", "low", "close", "volume"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        # Stored oldest-first: every downstream consumer (TimesFM, Kronos) wants
        # chronological order. The desc request is purely to pin the window end.
        return frame.sort_values("timestamp").reset_index(drop=True)

    # ---------------------------------------------------------------- options
    def options_chain(self, underlying: str, max_dte: int) -> pd.DataFrame:
        rows = self._chain_rows(underlying, 0, max_dte)
        if not rows:
            log.warning("lse-data options(): empty chain for '%s' (max_dte=%d)", underlying, max_dte)
            return _empty_options()

        # Keys verified against a live lse-data 0.14.0 response (2026-09-22):
        #   contract_type delta dte expiry gamma iv last_price last_trade_at
        #   premium_today rho strike theta ticker underlying underlying_price
        #   updated_at vega volume_today
        # Identity fields must be present and non-null. Pricing fields may be
        # null: an option that has not traded has no IV or greeks yet, and a
        # null there is data, not a mapping error.
        ctx = "lse options"

        def req(r, keys):
            return pick(r, keys, context=ctx)

        def val(r, keys):
            return pick(r, keys, context=ctx, allow_null=True)

        frame = pd.DataFrame(
            {
                "underlying": underlying,
                "contract": [req(r, ("ticker", "symbol", "osi", "contract")) for r in rows],
                "expiry": [req(r, ("expiry", "expiration", "expiry_date")) for r in rows],
                "dte": [val(r, ("dte",)) for r in rows],
                "strike": [req(r, ("strike", "strike_price")) for r in rows],
                "type": [req(r, ("contract_type", "type", "right")) for r in rows],
                "last_price": [val(r, ("last_price", "price", "last")) for r in rows],
                "underlying_price": [val(r, ("underlying_price",)) for r in rows],
                "implied_volatility": [val(r, ("iv", "implied_volatility")) for r in rows],
                "delta": [val(r, ("delta",)) for r in rows],
                "gamma": [val(r, ("gamma",)) for r in rows],
                "theta": [val(r, ("theta",)) for r in rows],
                "vega": [val(r, ("vega",)) for r in rows],
                "rho": [val(r, ("rho",)) for r in rows],
                # The live payload calls it volume_today. Never default a missing
                # volume to 0 — that silently reports every contract as untraded.
                "volume": [val(r, ("volume_today", "volume")) for r in rows],
                "premium": [val(r, ("premium_today", "premium")) for r in rows],
                "open_interest": [pick_optional(r, ("open_interest", "oi")) for r in rows],
                "updated_at": [pick_optional(r, ("updated_at", "last_trade_at")) for r in rows],
            }
        )
        return normalize_options(frame)

    def _chain_rows(self, underlying: str, min_dte: int, max_dte: int) -> list[dict[str, Any]]:
        """Fetch a chain without silently truncating it.

        options() has no ordering and the server clamps every response to 5000
        rows, so a capped response is an arbitrary subset of the chain — the
        first live NVDA pull came back at exactly 5000. When a DTE window hits
        the cap, split it in half and fetch each half; a single day that still
        hits the cap is a hard error rather than a quietly partial chain.
        """
        calls = 0

        def fetch(lo: int, hi: int) -> list[dict[str, Any]]:
            nonlocal calls
            calls += 1
            return self._call("options", underlying=underlying, min_dte=lo, max_dte=hi,
                              limit=_MAX_ROWS)

        def window(lo: int, hi: int, rows: list[dict[str, Any]] | None = None):
            rows = fetch(lo, hi) if rows is None else rows
            if len(rows) < _MAX_ROWS:
                return rows
            if lo >= hi:
                raise ProviderError(
                    f"lse options({underlying}): expiries at {lo} DTE alone reach the "
                    f"{_MAX_ROWS}-row cap, so the chain would be truncated. Lower "
                    "data.options_max_dte or add strike paging."
                )
            mid = (lo + hi) // 2
            return window(lo, mid) + window(mid + 1, hi)

        first = fetch(min_dte, max_dte)
        if len(first) >= _MAX_ROWS:
            # Paging splits [lo, hi] into [lo, mid] + [mid+1, hi], which is only
            # lossless if both bounds are inclusive. The docs do not say, so ask
            # the server directly: a one-day window at a DTE we know has
            # contracts must come back non-empty.
            probe = next((r.get("dte") for r in first if r.get("dte") is not None), None)
            if probe is None:
                raise ProviderError(f"lse options({underlying}): capped chain has no 'dte' "
                                    "field to page on — refusing a partial chain")
            if not fetch(int(probe), int(probe)):
                raise ProviderError(
                    f"lse options({underlying}): a [{int(probe)}, {int(probe)}] DTE window "
                    "came back empty although contracts at that DTE exist, so min_dte/max_dte "
                    "are not inclusive. Paging would lose boundary expiries — refusing."
                )
        rows = window(min_dte, max_dte, first)

        # Windows are disjoint, but DTE can tick over mid-run; key on the contract.
        unique: dict[str, dict[str, Any]] = {}
        for row in rows:
            unique.setdefault(str(pick(row, ("ticker", "symbol", "osi", "contract"),
                                       context="lse options")), row)
        if calls > 1:
            if len(unique) <= _MAX_ROWS:
                # Splitting a capped window must recover MORE than the cap. If the
                # windows all returned the same contracts, the server is ignoring
                # the DTE filter and paging cannot reach the rest of the chain.
                raise ProviderError(
                    f"lse options({underlying}): paged {calls} DTE windows but recovered "
                    f"only {len(unique)} distinct contracts — expected more than {_MAX_ROWS}. "
                    "The server appears to ignore min_dte/max_dte; refusing a partial chain."
                )
            log.info("lse options(%s): %d contracts across %d DTE windows",
                     underlying, len(unique), calls)
        return list(unique.values())

    # ------------------------------------------------------------------ macro
    def macro_series(self, cpi_series: str, yield_series: str) -> pd.DataFrame:
        parts = [
            self._series_frame("economics", cpi_series, "CPI"),
            self._series_frame("bond_yields", yield_series, "YIELD_10Y"),
        ]
        present = [p for p in parts if not p.empty]
        if not present:
            return _empty_macro()
        return pd.concat(present, ignore_index=True).sort_values(["series", "timestamp"]).reset_index(drop=True)

    def _series_frame(self, method: str, symbol: str, label: str) -> pd.DataFrame:
        if not symbol:
            return _empty_macro()
        try:
            rows = self._call(method, symbol=symbol, order="desc", limit=_MAX_ROWS)
        except ProviderError as exc:
            # A wrong series code should not sink the whole data stage; the macro
            # artifact records the gap and `--discover-macro` lists valid codes.
            log.warning("macro %s (%s via %s) unavailable: %s", label, symbol, method, exc)
            return _empty_macro()
        if not rows:
            log.warning("macro %s (%s via %s) returned no rows", label, symbol, method)
            return _empty_macro()

        ctx = f"lse {method}({symbol})"
        assert_descending(rows, _time_key(rows[0], ctx), context=ctx)
        time_key = _time_key(rows[0], ctx)
        frame = pd.DataFrame(
            {
                "timestamp": [r.get(time_key) for r in rows],
                "series": label,
                "source_symbol": symbol,
                "value": [pick(r, ("value", "close", "actual", "rate"), context=ctx,
                                allow_null=True) for r in rows],
            }
        )
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, format="mixed")
        frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
        frame = frame.dropna(subset=["value"])
        if frame["timestamp"].duplicated().any():
            dupes = int(frame["timestamp"].duplicated().sum())
            raise ProviderError(
                f"{ctx}: {dupes} dates appear more than once, so this code returned "
                "more than one series. Check it with: python run.py --discover-macro"
            )
        if len(rows) >= _MAX_ROWS:
            # We asked newest-first, so the cap only cuts the oldest history.
            log.info("%s: %d-row cap reached; history starts %s (older rows not fetched)",
                     ctx, _MAX_ROWS, frame["timestamp"].min().date())
        return frame.sort_values("timestamp").reset_index(drop=True)

    # -------------------------------------------------------------- discovery
    def fundamentals_rows(self, symbol: str) -> list[dict[str, Any]]:
        """Raw fundamentals snapshot (market cap, PE, margins...) — shape not yet mapped."""
        return self._call("fundamentals", symbol=symbol)

    def market_cap_snapshot(self, symbol: str) -> dict[str, Any]:
        """Market cap in raw USD with the price it was computed at.

        The snapshot is not refreshed with the bars (live run: its price was the
        previous session's close), so stage 2 compares implied share counts
        (cap / price), never raw caps from different days.
        """
        context = f"lse fundamentals({symbol})"
        rows = self.fundamentals_rows(symbol)
        if not rows:
            raise ProviderError(f"{context}: no rows")
        row = rows[0]
        cap = pick(row, ["market_cap"], context=context)
        price = pick(row, ["current_price"], context=context, allow_null=True)
        as_of = pick(row, ["updated_at"], context=context, allow_null=True)
        return {"market_cap": float(cap), "price": None if price is None else float(price),
                "as_of": None if as_of is None else str(as_of), "source": "lse"}

    def next_earnings(self, symbol: str) -> dict[str, Any]:
        from .yf_fred import yahoo_next_earnings
        return yahoo_next_earnings(symbol)

    def list_economics(self) -> list[dict[str, Any]]:
        """Catalogue of macro series — used by `run.py --discover-macro`."""
        return self._call("economics")


def _time_key(row: dict, context: str) -> str:
    for key in ("timestamp", "ts", "date", "period", "datetime"):
        if key in row:
            return key
    raise ProviderError(f"{context}: no time column found. Actual keys: {sorted(row)}")


def _empty_options() -> pd.DataFrame:
    return normalize_options(pd.DataFrame(columns=list(OPTIONS_COLUMNS)))


def _empty_macro() -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": pd.Series(dtype="datetime64[ns, UTC]"),
        "series": pd.Series(dtype="string"),
        "source_symbol": pd.Series(dtype="string"),
        "value": pd.Series(dtype="float64"),
    })
