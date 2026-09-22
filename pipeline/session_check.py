"""What does a vendor's daily bar cover: the regular session, or the extended day?

Motivation: LSE's daily close for a finished day kept changing through the
evening, and differed from TradingView's regular-session close by up to ~0.8%
on some past days while agreeing to a few cents on others. That pattern fits a
daily bar that includes after-hours trading. Rebuilding one day from 5-minute
candles shows which it is, instead of assuming.
"""
from __future__ import annotations

from datetime import date, time
from zoneinfo import ZoneInfo

import pandas as pd

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
CENT = 0.011


def session_summary(intraday: pd.DataFrame, daily: dict | None, day: date,
                    market_tz: str = "America/New_York") -> dict:
    """Summarise one trading day from intraday bars (bar-open timestamps, UTC)
    and say which part of it the daily bar matches."""
    tz = ZoneInfo(market_tz)
    bars = intraday.copy()
    bars["local"] = pd.to_datetime(bars["timestamp"], utc=True).dt.tz_convert(tz)
    bars = bars[bars["local"].dt.date == day].sort_values("local")
    if bars.empty:
        return {"day": day.isoformat(), "error": "no intraday bars for this day"}

    clock = bars["local"].dt.time
    regular = bars[(clock >= REGULAR_OPEN) & (clock < REGULAR_CLOSE)]
    pre = bars[clock < REGULAR_OPEN]
    post = bars[clock >= REGULAR_CLOSE]

    def part(frame: pd.DataFrame) -> dict | None:
        if frame.empty:
            return None
        return {"first_bar": frame["local"].iloc[0].strftime("%H:%M"),
                "last_bar": frame["local"].iloc[-1].strftime("%H:%M"),
                "open": float(frame["open"].iloc[0]), "close": float(frame["close"].iloc[-1]),
                "high": float(frame["high"].max()), "low": float(frame["low"].min()),
                "volume": float(frame["volume"].sum()), "bars": int(len(frame))}

    out = {"day": day.isoformat(), "regular": part(regular), "pre_market": part(pre),
           "after_hours": part(post), "whole_day": part(bars), "daily_bar": daily}

    if daily and out["regular"]:
        close = float(daily["close"])
        reg_close, last_close = out["regular"]["close"], out["whole_day"]["close"]
        matches = []
        if abs(close - reg_close) <= CENT:
            matches.append("regular-session close (16:00)")
        if abs(close - last_close) <= CENT and out["after_hours"]:
            matches.append(f"last extended-hours trade ({out['whole_day']['last_bar']})")
        out["daily_close_matches"] = matches or ["neither"]
        volume = float(daily.get("volume") or 0)
        reg_vol, all_vol = out["regular"]["volume"], out["whole_day"]["volume"]
        out["daily_volume_vs_regular_pct"] = round(100 * volume / reg_vol, 1) if reg_vol else None
        out["daily_volume_vs_whole_day_pct"] = round(100 * volume / all_vol, 1) if all_vol else None
    return out
