"""Regular-session daily bars rebuilt from 30-minute candles. Synthetic prices.

The shape follows what --discover-session showed live: bars run 04:00-20:00
New York, and the vendor's daily close is the last after-hours trade.
"""
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from pipeline.errors import ProviderError
from pipeline.providers.lse import LSEProvider, _MAX_ROWS
from pipeline.session_check import regular_session_daily, us_early_close

NY = "America/New_York"


def day_bars(day: date, regular_close=100.0, after_hours=105.0, close_at="16:00", skip=()):
    """04:00-20:00 30-minute bars. Regular bars trade around `regular_close` and
    the last regular bar closes exactly there; later bars trade at `after_hours`."""
    start = pd.Timestamp(f"{day} 04:00", tz=NY)
    times = pd.date_range(start, periods=32, freq="30min")
    cut = pd.Timestamp(f"{day} {close_at}", tz=NY)
    rows = []
    for t in times:
        if t.strftime("%H:%M") in skip:
            continue
        regular = pd.Timestamp(f"{day} 09:30", tz=NY) <= t < cut
        price = regular_close if regular else (after_hours if t >= cut else regular_close - 1)
        rows.append({"timestamp": t.tz_convert("UTC"), "open": price, "high": price + 1,
                     "low": price - 1, "close": price, "volume": 10.0 if regular else 1.0})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- half-days
@pytest.mark.parametrize("day,expected", [
    (date(2025, 11, 28), "13:00"),   # day after Thanksgiving
    (date(2026, 11, 27), "13:00"),
    (date(2026, 11, 26), None),      # Thanksgiving itself: closed, not a half-day
    (date(2025, 7, 3), "13:00"),     # Thursday
    (date(2026, 7, 3), None),        # Friday: the observed holiday
    (date(2026, 12, 24), "13:00"),   # Thursday
    (date(2021, 12, 24), None),      # Friday: the observed holiday
    (date(2026, 9, 22), None),
])
def test_us_half_days(day, expected):
    got = us_early_close(day)
    assert (got.strftime("%H:%M") if got else None) == expected


# -------------------------------------------------------------- aggregation
def test_daily_close_is_the_regular_close_not_the_after_hours_trade():
    bars = pd.concat([day_bars(date(2026, 5, 5), 100, 105), day_bars(date(2026, 5, 6), 101, 99)])
    daily, report = regular_session_daily(bars, "TEST", bar_minutes=30, market_tz=NY)
    assert daily["close"].tolist() == [100.0, 101.0]
    assert daily["volume"].tolist() == [130.0, 130.0]           # 13 regular bars x 10
    assert daily["timestamp"].dt.strftime("%Y-%m-%d").tolist() == ["2026-05-05", "2026-05-06"]
    assert str(daily["timestamp"].iloc[0].tz) == "UTC" and report["days"] == 2


def test_half_day_ends_at_13():
    bars = day_bars(date(2025, 11, 28), 100, 105, close_at="13:00")
    daily, report = regular_session_daily(bars, "TEST", bar_minutes=30, market_tz=NY)
    assert daily["close"].tolist() == [100.0] and daily["volume"].tolist() == [70.0]
    assert report["early_close_days"] == ["2025-11-28"]


def test_past_day_without_its_closing_bar_is_dropped_but_the_newest_is_kept():
    bars = pd.concat([day_bars(date(2026, 5, 5), skip=("15:30",)),
                      day_bars(date(2026, 5, 6)),
                      day_bars(date(2026, 5, 7), skip=("15:00", "15:30"))])   # still trading
    daily, report = regular_session_daily(bars, "TEST", bar_minutes=30, market_tz=NY)
    assert daily["timestamp"].dt.strftime("%Y-%m-%d").tolist() == ["2026-05-06", "2026-05-07"]
    assert report["dropped_incomplete"] == ["2026-05-05"]


def test_day_without_the_opening_bar_is_dropped_and_interior_gaps_are_reported():
    bars = pd.concat([day_bars(date(2026, 5, 5), skip=("09:30",)),
                      day_bars(date(2026, 5, 6), skip=("12:00",)),
                      day_bars(date(2026, 5, 7))])
    daily, report = regular_session_daily(bars, "TEST", bar_minutes=30, market_tz=NY)
    assert report["dropped_incomplete"] == ["2026-05-05"]
    assert report["days_with_gaps"] == ["2026-05-06"]
    assert len(daily) == 2


def test_bars_off_the_0930_grid_are_refused():
    bars = day_bars(date(2026, 5, 5))
    bars["timestamp"] = bars["timestamp"] + pd.Timedelta(minutes=15)
    with pytest.raises(ValueError, match="not aligned"):
        regular_session_daily(bars, "TEST", bar_minutes=30, market_tz=NY)


# ------------------------------------------------------ provider + windows
class _Err(Exception):
    pass


class FakeCandleServer:
    """Serves 30-minute bars for every weekday in a range, filtered by the
    start/end dates (inclusive) and clamped to the cap, newest first."""

    def __init__(self, first: date, last: date, cap=_MAX_ROWS):
        frames = [day_bars(first + timedelta(days=i))
                  for i in range((last - first).days + 1)
                  if (first + timedelta(days=i)).weekday() < 5]
        self.bars = pd.concat(frames, ignore_index=True)
        self.cap = cap
        self.calls = []

    def candles(self, symbol, timeframe, start, end, limit, order, **_):
        self.calls.append((start, end))
        day = self.bars["timestamp"].dt.date
        rows = self.bars[(day >= date.fromisoformat(start)) & (day <= date.fromisoformat(end))]
        rows = rows.sort_values("timestamp", ascending=(order == "asc")).head(min(limit, self.cap))
        return [{**r, "timestamp": r["timestamp"].isoformat()} for r in rows.to_dict("records")]


def _provider(server):
    p = LSEProvider.__new__(LSEProvider)
    p._client, p._LSEError = server, _Err
    p.daily_session, p.market_tz, p.session_report = "regular", NY, None
    return p


def test_lookback_is_fetched_in_windows_and_rebuilt_per_day():
    today = datetime.now(timezone.utc).date()
    server = FakeCandleServer(today - timedelta(days=300), today - timedelta(days=1))
    daily = _provider(server).daily_ohlcv("TEST", lookback_days=250)

    weekdays = sum(1 for i in range(1, 251) if (today - timedelta(days=i)).weekday() < 5)
    assert len(daily) == weekdays
    assert daily["timestamp"].is_monotonic_increasing and not daily["timestamp"].duplicated().any()
    assert len(server.calls) >= 3                              # 250 days / 120-day windows


def test_a_window_at_the_row_cap_is_an_error_not_a_truncation(monkeypatch):
    today = datetime.now(timezone.utc).date()
    server = FakeCandleServer(today - timedelta(days=60), today - timedelta(days=1), cap=100)
    monkeypatch.setattr("pipeline.providers.lse._MAX_ROWS", 100)
    with pytest.raises(ProviderError, match="row cap reached"):
        _provider(server).daily_ohlcv("TEST", lookback_days=50)


def test_extended_session_keeps_the_vendor_daily_bar():
    class DailyServer:
        def candles(self, symbol, timeframe, **_):
            assert timeframe == "1d"
            return [{"timestamp": "2026-05-06T00:00:00Z", "open": 1, "high": 2, "low": 0.5,
                     "close": 1.5, "volume": 10}]
    p = _provider(DailyServer())
    p.daily_session = "extended"
    assert p.daily_ohlcv("TEST", 5)["close"].tolist() == [1.5]
