"""Provider interface + shared ordering/freshness guards."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

import pandas as pd

from ..errors import OrderingError, ProviderError, StaleDataError

log = logging.getLogger(__name__)


class MarketDataProvider(ABC):
    """What the data stage needs. Implementations must never silently guess."""

    name: str = "base"

    @abstractmethod
    def daily_ohlcv(self, symbol: str, lookback_days: int) -> pd.DataFrame: ...

    @abstractmethod
    def options_chain(self, underlying: str, max_dte: int) -> pd.DataFrame: ...

    @abstractmethod
    def macro_series(self, cpi_series: str, yield_series: str) -> pd.DataFrame: ...

    # Reference values stage 2 cross-checks. Optional: None means "not offered".
    def market_cap_snapshot(self, symbol: str) -> dict[str, Any] | None:
        """{"market_cap": USD, "price": the price that cap was computed at, "as_of", "source"}"""
        return None

    def next_earnings(self, symbol: str) -> dict[str, Any] | None:
        """{"dates": [ISO date, ...] (one date, or a start/end window), "source"}"""
        return None


def pick(row: dict[str, Any], candidates: Sequence[str], *, context: str,
         allow_null: bool = False) -> Any:
    """Return the value of the first candidate key the row actually has.

    Missing and null are different failures and must not be reported as one:

    - a **missing** key means the payload shape changed or we mapped it wrong —
      always an error, because guessing a field is how a strike gets reported
      as a price;
    - a **null** value means the vendor has nothing for this row, which is
      legitimate for some fields (the IV of an option that never traded) and a
      data error for others (a bar with no close). `allow_null` says which.
    """
    for key in candidates:
        if key in row:
            value = row[key]
            if value is None and not allow_null:
                raise ProviderError(
                    f"{context}: '{key}' is present but null, and a null is not "
                    f"acceptable for it (row {_row_ident(row)})"
                )
            return value
    raise ProviderError(
        f"{context}: none of {list(candidates)} present in response row. "
        f"Actual keys: {sorted(row)}"
    )


def _row_ident(row: dict[str, Any]) -> dict[str, Any]:
    """The few fields that identify a row in an error message."""
    return {k: row[k] for k in ("ticker", "symbol", "timestamp", "expiry", "strike") if k in row}


def pick_optional(row: dict[str, Any], candidates: Sequence[str], default=None) -> Any:
    for key in candidates:
        if key in row and row[key] is not None:
            return row[key]
    return default


def assert_descending(rows: Iterable[dict], time_key: str, *, context: str) -> None:
    """Verify the provider actually honoured order="desc".

    lse-data's candles() defaults to order="asc" (oldest first). We always pass
    "desc" explicitly, but a server-side default change would silently hand us
    the *start* of history. Checking the response — rather than trusting the
    request — is the only way to catch that.
    """
    stamps = [r.get(time_key) for r in rows if r.get(time_key) is not None]
    if len(stamps) < 2:
        return
    parsed = pd.to_datetime(pd.Series(stamps), utc=True, format="mixed")
    if parsed.iloc[0] < parsed.iloc[-1]:
        raise OrderingError(
            f"{context}: asked for order='desc' but got ascending rows "
            f"(first={parsed.iloc[0]}, last={parsed.iloc[-1]}). "
            "The provider ignored the ordering argument — refusing to continue."
        )


def assert_fresh(df: pd.DataFrame, time_column: str, max_staleness_days: int, *, context: str) -> None:
    """The newest row must be recent. Catches 'we silently got 2003' outright."""
    if df.empty:
        raise ProviderError(f"{context}: no rows returned")
    newest = pd.to_datetime(df[time_column], utc=True).max()
    age_days = (datetime.now(timezone.utc) - newest.to_pydatetime()).days
    if age_days > max_staleness_days:
        raise StaleDataError(
            f"{context}: newest row is {newest.date()} ({age_days}d old), "
            f"tolerance is {max_staleness_days}d. Refusing to build on stale data."
        )
    log.debug("%s: freshness ok, newest=%s (%dd)", context, newest.date(), age_days)


def window_start(lookback_days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
