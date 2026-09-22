"""Which part of the day a daily bar covers. Synthetic 5-minute bars."""
from datetime import date

import pandas as pd

from pipeline.session_check import session_summary

DAY = date(2026, 5, 6)


def five_minute_day():
    """04:00-20:00 New York (EDT = UTC-4). Regular session ends at 50.00; the
    last after-hours trade is 51.00."""
    times = pd.date_range("2026-05-06 08:00", "2026-05-06 23:55", freq="5min", tz="UTC")
    local = times.tz_convert("America/New_York")
    close = [51.0 if t.hour >= 16 else 50.0 for t in local]
    volume = [10.0 if (t.hour, t.minute) >= (9, 30) and t.hour < 16 else 1.0 for t in local]
    return pd.DataFrame({"timestamp": times, "open": close, "high": [c + 0.5 for c in close],
                         "low": [c - 0.5 for c in close], "close": close, "volume": volume})


def test_regular_session_is_0930_to_1600_new_york():
    s = session_summary(five_minute_day(), None, DAY)
    assert s["regular"]["first_bar"] == "09:30" and s["regular"]["last_bar"] == "15:55"
    assert s["regular"]["bars"] == 78 and s["regular"]["close"] == 50.0
    assert s["after_hours"]["first_bar"] == "16:00" and s["whole_day"]["close"] == 51.0


def test_daily_bar_including_after_hours_is_recognised():
    bars = five_minute_day()
    whole_volume = float(bars["volume"].sum())
    s = session_summary(bars, {"close": 51.0, "volume": whole_volume}, DAY)
    assert s["daily_close_matches"] == ["last extended-hours trade (19:55)"]
    assert s["daily_volume_vs_whole_day_pct"] == 100.0


def test_regular_session_daily_bar_is_recognised():
    s = session_summary(five_minute_day(), {"close": 50.0, "volume": 780.0}, DAY)
    assert s["daily_close_matches"] == ["regular-session close (16:00)"]
    assert s["daily_volume_vs_regular_pct"] == 100.0


def test_day_without_bars_says_so():
    assert "error" in session_summary(five_minute_day(), None, date(2026, 5, 7))


def test_discover_session_command_prints_the_verdict(monkeypatch, capsys, tmp_path):
    import shutil
    from pathlib import Path

    import run
    from pipeline.config import load_settings

    root = Path(__file__).resolve().parent.parent
    shutil.copy(root / "config.yaml", tmp_path / "config.yaml")
    settings = load_settings(tmp_path / "config.yaml", root=tmp_path)
    bars = five_minute_day()

    class FakeLSE:
        def intraday(self, symbol, timeframe, start, end):
            if timeframe == "1d":
                return pd.DataFrame({"timestamp": [pd.Timestamp("2026-05-06", tz="UTC")],
                                     "open": [50.0], "high": [51.5], "low": [49.5],
                                     "close": [51.0], "volume": [float(bars["volume"].sum())]})
            return bars

    monkeypatch.setattr("pipeline.providers.get_provider", lambda name, settings=None: FakeLSE())
    assert run.discover_session(settings, "TEST", "2026-05-06") == 0
    out = capsys.readouterr().out
    assert "daily close matches: last extended-hours trade (19:55)" in out
    assert "regular      09:30-15:55" in out
