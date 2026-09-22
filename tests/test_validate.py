"""Stage 2: the three checks, the HALT they cause, and the gate file. Synthetic numbers."""
from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from pipeline.cache import make_run_context
from pipeline.config import load_settings
from pipeline.errors import ConfigError, PipelineHalt
from pipeline.gate import require_validation_pass
from pipeline.providers.tradingview_data import RateLimited, ShapeNotMapped, TradingViewData
from pipeline.stages.data import collect_reference
from pipeline.stages.validate import (
    FAIL,
    PASS,
    UNVERIFIABLE,
    ValidateStage,
    check_close,
    check_earnings,
    check_market_cap,
)

ROOT = Path(__file__).resolve().parent.parent


def bars(closes, start="2026-05-04"):
    days = pd.date_range(start, periods=len(closes), freq="B", tz="UTC")
    c = pd.Series(closes, dtype="float64")
    return pd.DataFrame({"timestamp": days, "symbol": "TEST", "open": c, "high": c + 1,
                         "low": c - 1, "close": c, "volume": 1000.0})


# ------------------------------------------------------------------ close
def test_close_within_tolerance_passes():
    c = check_close(bars([100, 101, 102]), bars([100, 101, 102.3]), 0.5)
    assert c["status"] == PASS and c["date"] == "2026-05-06" and c["overlap_days"] == 3
    assert [row["date"] for row in c["overlap"]] == ["2026-05-04", "2026-05-05", "2026-05-06"]
    assert c["overlap"][0]["diff_pct"] == 0 and c["overlap_max_diff_pct"] == c["overlap"][-1]["diff_pct"]


def test_close_beyond_tolerance_fails():
    c = check_close(bars([100, 101, 102]), bars([100, 101, 104]), 0.5)
    assert c["status"] == FAIL and c["diff_pct"] > 0.5


def test_close_missing_that_day_on_the_other_side_fails():
    c = check_close(bars([100, 101, 102]), bars([100, 101]), 0.5)
    assert c["status"] == FAIL and "no completed bar for 2026-05-06" in c["detail"]


def test_our_data_behind_the_other_source_fails():
    c = check_close(bars([100, 101]), bars([100, 101, 102]), 0.5)
    assert c["status"] == FAIL and "rerun the data stage" in c["detail"]


def test_no_bars_from_the_other_side_is_unverifiable():
    assert check_close(bars([100]), bars([]), 0.5)["status"] == UNVERIFIABLE


# ------------------------------------------------------------- market cap
def test_market_cap_compares_share_counts_not_caps_from_different_days():
    # Same share count; the caps differ by 5% only because the prices do.
    ours = {"market_cap": 1_000.0 * 100, "price": 100.0}
    theirs = {"market_cap": 1_000.0 * 105, "price": 105.0}
    assert check_market_cap(ours, theirs, 2.0)["status"] == PASS


def test_market_cap_share_count_mismatch_fails():
    ours = {"market_cap": 1_000.0 * 100, "price": 100.0}
    theirs = {"market_cap": 1_100.0 * 100, "price": 100.0}
    assert check_market_cap(ours, theirs, 2.0)["status"] == FAIL


def test_market_cap_without_a_price_is_unverifiable():
    ours = {"market_cap": 1e5, "price": None}
    assert check_market_cap(ours, {"market_cap": 1e5, "price": 1.0}, 2.0)["status"] == UNVERIFIABLE


# --------------------------------------------------------------- earnings
@pytest.mark.parametrize("ours,theirs,status", [
    (["2026-11-18"], "2026-11-18", PASS),
    (["2026-11-18"], "2026-11-19", PASS),
    (["2026-11-18"], "2026-11-20", FAIL),
    (["2026-11-16", "2026-11-20"], "2026-11-19", PASS),     # inside an estimated window
    (["2026-11-16", "2026-11-20"], "2026-11-22", FAIL),
])
def test_earnings_date_tolerance(ours, theirs, status):
    assert check_earnings({"dates": ours}, {"date": theirs}, 1)["status"] == status


# ------------------------------------------------------------------ stage
@pytest.fixture
def ctx(tmp_path):
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    settings = load_settings(tmp_path / "config.yaml", root=tmp_path)
    c = make_run_context(settings, "TEST", "2026-05-08")
    c.write_parquet("data_ohlcv", bars([100, 101, 102]))
    c.write_json("data_reference", {
        "market_cap": {"market_cap": 102_000.0, "price": 102.0, "source": "x"},
        "next_earnings": {"dates": ["2026-06-01"], "source": "x"},
    })
    return c


class FakeSource:
    def __init__(self, bars_df, cap=None, earnings=None, cap_error=None):
        self.bars_df, self.cap, self.earnings, self.cap_error = bars_df, cap, earnings, cap_error
        self.symbols = []

    def daily_bars(self, symbol, count):
        self.symbols.append(symbol)
        return self.bars_df

    def market_cap(self, symbol):
        if self.cap_error:
            raise self.cap_error
        return self.cap

    def next_earnings(self, symbol):
        return self.earnings


def agreeing_source():
    return FakeSource(bars([100, 101, 102]), {"market_cap": 103_000.0, "price": 103.0},
                      {"date": "2026-06-01"})


def test_all_checks_pass_opens_the_gate(ctx):
    source = agreeing_source()
    result = ValidateStage(lambda c: (source, "fake")).run(ctx)
    assert result.status == "ok"
    report = require_validation_pass(ctx)
    assert [c["status"] for c in report["checks"]] == [PASS, PASS, PASS]
    assert source.symbols == ["NASDAQ:TEST"]


def test_rate_limited_check_is_unverifiable_and_halts(ctx):
    source = agreeing_source()
    source.cap_error = RateLimited("mcp-tv-get-symbol-data: scan: 429 (still rate limited)")
    with pytest.raises(PipelineHalt, match="market_cap=unverifiable"):
        ValidateStage(lambda c: (source, "fake")).run(ctx)

    report = ctx.read_json("validation")
    assert report["status"] == FAIL
    cap = next(c for c in report["checks"] if c["name"] == "market_cap")
    assert cap["status"] == UNVERIFIABLE and "429" in cap["detail"]
    with pytest.raises(PipelineHalt, match="market_cap=unverifiable"):
        require_validation_pass(ctx)          # downstream stays blocked


def test_close_mismatch_halts(ctx):
    source = agreeing_source()
    source.bars_df = bars([100, 101, 110])
    with pytest.raises(PipelineHalt, match="last_close=fail"):
        ValidateStage(lambda c: (source, "fake")).run(ctx)


def test_missing_reference_value_is_unverifiable(ctx):
    ctx.write_json("data_reference", {"market_cap": {"error": "LSE down"},
                                      "next_earnings": {"dates": ["2026-06-01"]}})
    with pytest.raises(PipelineHalt):
        ValidateStage(lambda c: (agreeing_source(), "fake")).run(ctx)
    cap = next(c for c in ctx.read_json("validation")["checks"] if c["name"] == "market_cap")
    assert cap["status"] == UNVERIFIABLE and "LSE down" in cap["detail"]


def test_validate_without_data_halts(tmp_path):
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    c = make_run_context(load_settings(tmp_path / "config.yaml", root=tmp_path), "TEST", "2026-05-08")
    with pytest.raises(PipelineHalt, match="run the data stage first"):
        ValidateStage(lambda c: (agreeing_source(), "fake")).run(c)


# ---------------------------------------------------------- not guessing
def _ok(payload):
    return SimpleNamespace(is_error=False, structured_content=payload,
                           content=[SimpleNamespace(text=json.dumps(payload))])


def test_unmapped_success_is_saved_not_guessed(tmp_path):
    client = SimpleNamespace(call_tool=lambda name, args: _ok({"success": True, "data": [1]}))
    data = TradingViewData(client, dump_dir=tmp_path)
    with pytest.raises(ShapeNotMapped, match="not been mapped"):
        data.market_cap("NASDAQ:TEST")
    saved = json.loads((tmp_path / "mcp-tv-get-symbol-data.json").read_text())
    assert saved == {"success": True, "data": [1]}


# ------------------------------------------------------ reference + config
def test_reference_failures_are_recorded_not_raised():
    class Provider:
        name = "p"

        def market_cap_snapshot(self, symbol):
            raise RuntimeError("boom")

        def next_earnings(self, symbol):
            return None

    ref = collect_reference(Provider(), "TEST")
    assert "boom" in ref["market_cap"]["error"]
    assert "does not offer" in ref["next_earnings"]["error"]


def test_tradingview_symbol_mapping(ctx):
    v = ctx.settings.validate
    assert v.tradingview_symbol("NVDA") == "NASDAQ:NVDA"
    mapped = dataclasses.replace(v, tradingview_symbols={"BRK-B": "NYSE:BRK.B"})
    assert mapped.tradingview_symbol("BRK-B") == "NYSE:BRK.B"


def test_unknown_validate_provider_is_rejected(ctx):
    with pytest.raises(ConfigError):
        dataclasses.replace(ctx.settings.validate, provider="finnhub")
