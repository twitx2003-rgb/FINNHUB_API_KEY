"""Session completeness — modelled on the first live Windows run (2026-09-22)."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from pipeline.market_hours import drop_incomplete_session

NY = ZoneInfo("America/New_York")


def _nvda_live_run():
    """Three bars shaped like the mid-session run that exposed the bug.

    Values are synthetic (vendor data is not redistributed in this repo); what
    matters is the shape: the newest bar is today's, with about half a normal
    day's volume. Labels are 00:00 New York time, converted to UTC, as the
    yfinance provider stores them.
    """
    days = pd.to_datetime(["2026-09-18", "2026-09-21", "2026-09-22"]).tz_localize(NY).tz_convert("UTC")
    return pd.DataFrame({
        "timestamp": days,
        "symbol": "NVDA",
        "open": [100.0, 102.0, 104.0],
        "high": [103.0, 105.0, 106.0],
        "low": [99.0, 101.0, 103.0],
        "close": [102.0, 104.0, 105.5],
        "volume": [120_000_000, 100_000_000, 50_000_000],
    })


def _drop(df, now):
    return drop_incomplete_session(df, market_tz="America/New_York",
                                   session_close="16:15", now=now)


def test_drops_the_bar_of_a_session_still_trading():
    """The real case: run at 14:23 New York, newest bar had half a day's volume."""
    out, dropped = _drop(_nvda_live_run(), datetime(2026, 9, 22, 14, 23, tzinfo=NY))
    assert dropped is not None
    assert dropped.date == "2026-09-22"
    assert dropped.close == 105.5
    assert len(out) == 2
    assert out.iloc[-1]["close"] == 104.0   # Monday's close is now the latest


def test_keeps_the_bar_after_the_session_has_closed():
    out, dropped = _drop(_nvda_live_run(), datetime(2026, 9, 22, 16, 30, tzinfo=NY))
    assert dropped is None
    assert len(out) == 3


def test_keeps_yesterdays_bar_before_todays_open():
    df = _nvda_live_run().iloc[:-1]          # newest bar is Monday
    out, dropped = _drop(df, datetime(2026, 9, 22, 8, 0, tzinfo=NY))
    assert dropped is None
    assert len(out) == 2


def test_uses_exchange_time_not_the_callers_clock():
    """21:23 in Israel is 14:23 in New York — still open."""
    israel = datetime(2026, 9, 22, 21, 23, tzinfo=ZoneInfo("Asia/Jerusalem"))
    _, dropped = _drop(_nvda_live_run(), israel)
    assert dropped is not None


def test_handles_utc_midnight_labels_from_lse():
    """lse-data stamps daily bars at 00:00 UTC; the date must not shift a day."""
    df = _nvda_live_run()
    df["timestamp"] = pd.to_datetime(["2026-09-18", "2026-09-21", "2026-09-22"], utc=True)
    _, dropped = _drop(df, datetime(2026, 9, 22, 11, 0, tzinfo=NY))
    assert dropped is not None and dropped.date == "2026-09-22"


def test_empty_frame_is_left_alone():
    out, dropped = _drop(_nvda_live_run().iloc[0:0], datetime(2026, 9, 22, 12, 0, tzinfo=NY))
    assert dropped is None and out.empty
