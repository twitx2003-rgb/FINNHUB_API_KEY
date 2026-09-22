"""Tool-result checking for TradingView data. Payload shapes copy the live server;
all numbers are synthetic."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from pipeline.errors import ContractError, ProviderError
from pipeline.market_hours import drop_incomplete_session
from pipeline.providers.tradingview_data import (
    RateLimited,
    ToolFailed,
    TradingViewData,
    bars_frame,
    tool_payload,
)

SYMBOL = "NASDAQ:TEST"
DAY = 86_400
T0 = int(datetime(2026, 5, 28, 13, 30, tzinfo=timezone.utc).timestamp())  # a session open


def result(payload=None, *, is_error=False, text=None):
    text = text if text is not None else json.dumps(payload)
    return SimpleNamespace(is_error=is_error, structured_content=payload,
                           content=[SimpleNamespace(text=text)])


def bar(i, close=100.0):
    return {"t": T0 + i * DAY, "o": close - 1, "h": close + 2, "l": close - 3, "c": close,
            "v": 1_000_000 + i}


def ohlcv_payload(bars, symbol=SYMBOL):
    return {"success": True, "symbol": symbol, "interval": "1D", "count": len(bars),
            "bars": bars}


class StubClient:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def call_tool(self, name, args):
        self.calls.append((name, args))
        return self.results.pop(0)


# ---------------------------------------------------------------- tool_payload
def test_success_false_is_a_failure_even_though_is_error_is_false():
    with pytest.raises(ToolFailed, match="symbol not found"):
        tool_payload(result({"success": False, "error": "symbol not found"}), "t")


def test_429_from_the_scanner_is_a_rate_limit():
    live_shape = {"success": False,
                  "error": "tradingview api: https://scanner.tradingview.com/america/scan: 429: "}
    with pytest.raises(RateLimited):
        tool_payload(result(live_shape), "t")


def test_a_number_containing_429_is_not_a_rate_limit():
    with pytest.raises(ToolFailed) as info:
        tool_payload(result({"success": False, "error": "bad id 14290"}), "t")
    assert not isinstance(info.value, RateLimited)


def test_mcp_level_error_is_a_failure():
    with pytest.raises(ToolFailed, match="boom"):
        tool_payload(result(None, is_error=True, text="boom"), "t")


def test_text_json_is_used_when_there_is_no_structured_content():
    r = result(None, text='{"success": true, "x": 1}')
    assert tool_payload(r, "t") == {"success": True, "x": 1}


def test_non_json_and_non_object_results_are_refused():
    with pytest.raises(ProviderError, match="not JSON"):
        tool_payload(result(None, text="hello"), "t")
    with pytest.raises(ProviderError, match="JSON object"):
        tool_payload(result([1, 2]), "t")


# ------------------------------------------------------------------ retry
def test_rate_limit_is_retried_then_succeeds():
    limited = result({"success": False, "error": "scan: 429: "})
    ok = result({"success": True, "v": 1})
    slept = []
    data = TradingViewData(StubClient(limited, limited, ok), delays=(1, 2, 3), sleep=slept.append)
    assert data.fetch("t", {}) == {"success": True, "v": 1}
    assert slept == [1, 2]


def test_rate_limit_that_never_clears_fails_loudly():
    limited = result({"success": False, "error": "scan: 429: "})
    data = TradingViewData(StubClient(*[limited] * 3), delays=(1, 2), sleep=lambda s: None)
    with pytest.raises(RateLimited, match="after 3 attempts"):
        data.fetch("t", {})


def test_other_failures_are_not_retried():
    client = StubClient(result({"success": False, "error": "invalid symbol"}))
    data = TradingViewData(client, delays=(1,), sleep=lambda s: pytest.fail("slept"))
    with pytest.raises(ToolFailed):
        data.fetch("t", {})
    assert len(client.calls) == 1


# ---------------------------------------------------------------- bars_frame
def test_bars_become_the_ohlcv_layout_oldest_first():
    df = bars_frame(ohlcv_payload([bar(0, 100), bar(1, 101), bar(4, 103)]), SYMBOL)
    assert list(df.columns) == ["timestamp", "symbol", "open", "high", "low", "close", "volume"]
    assert df["close"].tolist() == [100, 101, 103]
    assert str(df["timestamp"].iloc[0].tz) == "UTC"


def test_daily_bars_asks_for_daily_interval():
    client = StubClient(result(ohlcv_payload([bar(0), bar(1)])))
    TradingViewData(client).daily_bars(SYMBOL, count=2)
    assert client.calls == [("mcp-tv-get-ohlcv", {"symbol": SYMBOL, "interval": "1D", "count": 2})]


@pytest.mark.parametrize("payload,match", [
    (ohlcv_payload([bar(1), bar(0)]), "ascending"),
    (ohlcv_payload([bar(0), {**bar(0), "t": T0 + 3600}]), "more than one bar"),
    (ohlcv_payload([]), "no bars"),
    (ohlcv_payload([{"t": T0, "o": 1, "h": 2, "l": 0.5}]), r"\['c'\]"),
    (ohlcv_payload([bar(0)], symbol="NASDAQ:OTHER"), "server answered for NASDAQ:OTHER"),
    ({"success": True, "symbol": SYMBOL}, r"\['bars'\]"),
])
def test_bad_bar_payloads_are_refused(payload, match):
    with pytest.raises(ProviderError, match=match):
        bars_frame(payload, SYMBOL)


def test_impossible_bar_is_refused():
    broken = {**bar(0), "h": 50.0}         # high below the close
    with pytest.raises(ContractError):
        bars_frame(ohlcv_payload([broken]), SYMBOL)


def test_unfinished_session_bar_from_tradingview_is_dropped():
    # The live server returned today's bar while the session was open.
    df = bars_frame(ohlcv_payload([bar(0), bar(1)]), SYMBOL)
    during_session = datetime.fromtimestamp(T0 + DAY + 3 * 3600, timezone.utc)
    kept, dropped = drop_incomplete_session(df, market_tz="America/New_York",
                                            session_close="16:15", now=during_session)
    assert len(kept) == 1 and dropped is not None
