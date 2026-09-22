"""Session completeness.

A daily bar fetched while the exchange is still open is not a close: its "close"
is the last trade so far and its volume covers part of the day. Letting it
through would make stage 2 compare a live price against another source's last
close, and would feed half a day of volume into the phase 3 volume forecast.

The first live run hit exactly this: run in the early afternoon New York time,
the newest bar carried roughly half a normal day's volume.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pandas as pd


@dataclass(frozen=True)
class DroppedBar:
    date: str
    close: float
    volume: float
    market_time: str   # wall-clock time at the exchange when we decided

    def as_dict(self) -> dict:
        return {"date": self.date, "close": self.close, "volume": self.volume,
                "market_time": self.market_time}


def drop_incomplete_session(
    df: pd.DataFrame,
    *,
    market_tz: str,
    session_close: str,
    now: datetime | None = None,
) -> tuple[pd.DataFrame, DroppedBar | None]:
    """Remove today's bar if the exchange has not closed yet.

    Bar dates are read as the UTC calendar date of `timestamp`, which matches the
    label both providers use (lse-data stamps 00:00 UTC; yfinance stamps 00:00
    New York, which is 04:00 UTC the same day).

    `session_close` should sit a little after the official close so the closing
    auction has printed. On early-close days (e.g. the day after Thanksgiving)
    the bar is held back until this time too — a few hours of extra staleness,
    never a partial bar.
    """
    if df.empty:
        return df, None

    tz = ZoneInfo(market_tz)
    now_local = (now or datetime.now(tz)).astimezone(tz)
    close_h, close_m = (int(part) for part in session_close.split(":"))

    last = df.iloc[-1]
    last_date = pd.Timestamp(last["timestamp"]).tz_convert("UTC").date()

    if last_date != now_local.date() or now_local.time() >= time(close_h, close_m):
        return df, None

    dropped = DroppedBar(
        date=last_date.isoformat(),
        close=float(last["close"]),
        volume=float(last["volume"]),
        market_time=now_local.strftime("%Y-%m-%d %H:%M %Z"),
    )
    return df.iloc[:-1].reset_index(drop=True), dropped
