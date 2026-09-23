"""US trading sessions: regular-session daily bars, half-days and holidays.

LSE's daily bar covers the whole 04:00-20:00 New York day (verified live on
2026-09-22 by rebuilding days from 5-minute candles: its close was the last
after-hours trade). The pipeline uses the regular session, rebuilt here from
intraday candles, and dates forecasts on scheduled trading days.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
# ------------------------------------------------ regular-session daily bars
# Confirmed live (run.py --discover-session, two days): LSE's daily bar is the
# whole 04:00-20:00 New York day — its close is the last after-hours trade and
# its volume includes pre-market and after-hours. The pipeline wants the
# regular session, so daily bars are rebuilt from intraday candles.

EARLY_CLOSE = time(13, 0)


def us_early_close(day: date) -> time | None:
    """13:00 on the three regular US half-days, else None.

    Rule-based, not a vendor calendar: the day after Thanksgiving, and July 3 /
    December 24 when they fall Monday-Thursday (on a Friday they are the
    observed holiday and the market is closed). On these days everything after
    13:00 is after-hours, so a fixed 16:00 cut-off would mix it in.
    """
    if day.month == 11 and day.weekday() == 4:
        first_thursday = 1 + (3 - date(day.year, 11, 1).weekday()) % 7
        if day.day == first_thursday + 22:          # fourth Thursday + 1
            return EARLY_CLOSE
    if (day.month, day.day) in ((7, 3), (12, 24)) and day.weekday() <= 3:
        return EARLY_CLOSE
    return None


def regular_session_daily(intraday: pd.DataFrame, symbol: str, *, bar_minutes: int,
                          market_tz: str = "America/New_York") -> tuple[pd.DataFrame, dict]:
    """Daily OHLCV from the regular session only (09:30 to 16:00, or 13:00).

    `intraday` has bar-open timestamps. Returns the daily frame (timestamp at
    00:00 UTC of the session date, like the vendor's daily bars) and a report of
    days dropped or kept with gaps. A past day without its first or last regular
    bar is dropped: its open or close would be wrong. The newest day may lack its
    last bar — the session may still be running — and is left for the data
    stage's clock-based check to judge.
    """
    report = {"days": 0, "dropped_incomplete": [], "days_with_gaps": [], "early_close_days": []}
    columns = ["timestamp", "symbol", "open", "high", "low", "close", "volume"]
    if intraday.empty:
        return pd.DataFrame(columns=columns), report

    tz = ZoneInfo(market_tz)
    bars = intraday.copy()
    bars["local"] = pd.to_datetime(bars["timestamp"], utc=True).dt.tz_convert(tz)
    bars["day"] = bars["local"].dt.date
    bars = bars.sort_values("local")

    minutes = bars["local"].dt.hour * 60 + bars["local"].dt.minute
    off_grid = bars[((minutes - (9 * 60 + 30)) % bar_minutes != 0) | (bars["local"].dt.second != 0)]
    if not off_grid.empty:
        raise ValueError(f"{symbol}: {bar_minutes}-minute bars not aligned to the 09:30 grid "
                         f"(e.g. {off_grid['local'].iloc[0]})")

    newest = bars["day"].max()
    out = []
    for day, part in bars.groupby("day", sort=True):
        close_at = us_early_close(day) or REGULAR_CLOSE
        clock = part["local"].dt.time
        regular = part[(clock >= REGULAR_OPEN) & (clock < close_at)]
        if regular.empty:
            continue                                   # weekend or holiday
        if close_at != REGULAR_CLOSE:
            report["early_close_days"].append(day.isoformat())

        span = (close_at.hour * 60 + close_at.minute) - (9 * 60 + 30)
        expected = span // bar_minutes
        last_start = (datetime.combine(day, close_at) - timedelta(minutes=bar_minutes)).time()
        starts = set(regular["local"].dt.time)
        has_first, has_last = REGULAR_OPEN in starts, last_start in starts

        if not has_first or (not has_last and day != newest):
            report["dropped_incomplete"].append(day.isoformat())
            continue
        if has_last and len(regular) < expected:
            report["days_with_gaps"].append(day.isoformat())

        out.append({
            "timestamp": pd.Timestamp(day.isoformat(), tz="UTC"),
            "symbol": symbol,
            "open": float(regular["open"].iloc[0]),
            "high": float(regular["high"].max()),
            "low": float(regular["low"].min()),
            "close": float(regular["close"].iloc[-1]),
            "volume": float(regular["volume"].sum()),
        })

    report["days"] = len(out)
    return pd.DataFrame(out, columns=columns), report


# ------------------------------------------------------------ trading days
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = (date(year, month + 1, 1) if month < 12 else date(year + 1, 1, 1)) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    return date(year, month, (h + l - 7 * m + 114) % 31 + 1)


def _observed(day: date) -> date:
    """Saturday holidays move to Friday, Sunday ones to Monday."""
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def us_market_holidays(year: int) -> set[date]:
    """Full-day NYSE/Nasdaq closures by rule. Unscheduled closures (a national
    day of mourning, a storm) cannot be known in advance and are not here."""
    days = {
        _nth_weekday(year, 1, 0, 3),                 # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),                 # Washington's Birthday
        _easter(year) - timedelta(days=2),           # Good Friday
        _last_weekday(year, 5, 0),                   # Memorial Day
        _observed(date(year, 7, 4)),                 # Independence Day
        _nth_weekday(year, 9, 0, 1),                 # Labor Day
        _nth_weekday(year, 11, 3, 4),                # Thanksgiving
        _observed(date(year, 12, 25)),               # Christmas
    }
    new_year = date(year, 1, 1)
    if new_year.weekday() != 5:                      # a Saturday New Year is not moved back
        days.add(_observed(new_year))
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))       # Juneteenth
    return days


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in us_market_holidays(day.year)


def next_sessions(after: date, count: int) -> list[date]:
    """The next `count` scheduled trading days after `after`."""
    out, day = [], after
    while len(out) < count:
        day += timedelta(days=1)
        if is_trading_day(day):
            out.append(day)
    return out
