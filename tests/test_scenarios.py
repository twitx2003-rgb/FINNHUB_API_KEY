"""Phase 5: Kronos scenario ranges — candle checks, degradation, backtest, adapter.

No PyTorch and no weights: the adapter runs against fakes shaped like the
vendored Kronos API (pipeline/vendor/kronos/kronos.py).
"""
from __future__ import annotations

import dataclasses
import shutil
import sys
import types
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pipeline import contracts
from pipeline.cache import make_run_context
from pipeline.config import ForecastSettings, load_settings
from pipeline.errors import ConfigError, ContractError, ProviderError
from pipeline.scenarios import (
    CANDLE,
    KronosSampler,
    SyntheticSampler,
    backtest,
    summarize,
    valid_candles,
)
from pipeline.session_check import is_trading_day, next_sessions
from pipeline.stages.forecast import ForecastStage, backtest_caveats

ROOT = Path(__file__).resolve().parent.parent


def good_paths(n_paths=20, horizon=5, start=100.0):
    close = start + np.arange(1, horizon + 1)[None, :] + np.arange(n_paths)[:, None] * 0.1
    open_ = close - 0.5
    return np.stack([open_, close + 1, open_ - 1, close, np.full_like(close, 1e6)], axis=-1)


def bars(n=200):
    days = [d for d in pd.bdate_range("2025-11-01", "2026-09-22") if is_trading_day(d.date())][-n:]
    price = pd.Series(np.linspace(100, 120, len(days)))
    return pd.DataFrame({
        "timestamp": pd.DatetimeIndex(days).tz_localize("UTC"), "symbol": "TEST",
        "open": price, "high": price + 1, "low": price - 1, "close": price,
        "volume": 1e6 + 1e5 * np.sin(np.arange(len(days)))})


@pytest.fixture
def ctx(tmp_path):
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    settings = load_settings(tmp_path / "config.yaml", root=tmp_path)
    settings = dataclasses.replace(settings, forecast=dataclasses.replace(
        settings.forecast, timesfm_enabled=False))
    c = make_run_context(settings, "TEST", "2026-09-22")
    c.write_parquet("data_ohlcv", bars())
    c.write_json("validation", {"status": "pass", "checks": []})
    return c


# ------------------------------------------------------------- candle rules
@pytest.mark.parametrize("column,value", [
    ("high", 50.0),          # high below the body
    ("low", 500.0),          # low above the body
    ("volume", -1.0),
    ("open", 0.0),           # non-positive price
    ("close", np.nan),
    ("volume", np.inf),
])
def test_each_broken_rule_marks_only_that_candle(column, value):
    paths = good_paths(3, 4)
    paths[1, 2, CANDLE.index(column)] = value
    valid = valid_candles(paths)
    assert not valid[1, 2] and valid.sum() == valid.size - 1


def test_real_bars_still_fail_loudly_with_the_rule_named():
    df = pd.DataFrame({"open": [10.0], "high": [9.0], "low": [8.0], "close": [10.0], "volume": [1.0]})
    with pytest.raises(ContractError, match=r"high < max\(open, close\)"):
        contracts.assert_ohlcv_sane(df)


# ------------------------------------------------------------------ summary
def test_invalid_candles_are_dropped_and_counted_not_repaired():
    paths = good_paths(20, 5)
    paths[0, :, CANDLE.index("low")] = 1e9          # path 0: every candle impossible
    s = summarize(paths, last_close=100, interval=(0.1, 0.9), max_invalid_pct=20, context="t")
    assert s["invalid_candles"] == 5 and s["invalid_pct"] == 5.0 and not s["degraded"]
    assert s["invalid_by_rule"] == {"low > min(open, close)": 5}
    assert [step["valid_paths"] for step in s["steps"]] == [19] * 5
    closes = paths[1:, 0, CANDLE.index("close")]
    assert s["steps"][0]["close_low"] == pytest.approx(np.quantile(closes, 0.1))
    assert s["steps"][0]["close_median"] == pytest.approx(np.median(closes))


def test_more_than_the_limit_invalid_is_degraded():
    paths = good_paths(20, 5)
    paths[:5, :, CANDLE.index("volume")] = -1        # 25% of candles
    s = summarize(paths, last_close=100, interval=(0.1, 0.9), max_invalid_pct=20, context="t")
    assert s["invalid_pct"] == 25.0 and s["degraded"]
    at_limit = good_paths(20, 5)
    at_limit[:4, :, CANDLE.index("volume")] = -1     # exactly 20%: not above the limit
    assert not summarize(at_limit, last_close=100, interval=(0.1, 0.9), max_invalid_pct=20,
                         context="t")["degraded"]


def test_a_step_without_enough_valid_candles_fails_the_stage():
    paths = good_paths(10, 3)
    paths[:6, 2, CANDLE.index("close")] = np.nan
    with pytest.raises(ProviderError, match="step 3 has 4 valid candles"):
        summarize(paths, last_close=100, interval=(0.1, 0.9), max_invalid_pct=20, context="t")


def test_final_change_and_share_up_are_measured_from_the_last_close():
    s = summarize(good_paths(20, 5, start=100), last_close=200, interval=(0.1, 0.9),
                  max_invalid_pct=20, context="t")
    assert s["share_of_paths_up_pct"] == 0.0 and s["final_close_change_pct"]["median"] < 0


# ----------------------------------------------------------------- backtest
class RecordingSampler(SyntheticSampler):
    def __init__(self):
        super().__init__()
        self.seen = []

    def sample(self, histories, future_dates, n_paths):
        self.seen = [(h["timestamp"].iloc[-1], len(h), list(d)) for h, d in zip(histories, future_dates)]
        return super().sample(histories, future_dates, n_paths)


def test_backtest_never_shows_the_model_the_future():
    data = bars(200)
    sampler = RecordingSampler()
    bt = backtest(data, sampler, windows=8, horizon=3, n_paths=12, context_length=100,
                  interval=(0.1, 0.9), max_invalid_pct=20, sessions_after=next_sessions)
    assert bt["windows"] == 8 and bt["points"] == 24 and bt["coverage_target_pct"] == 80
    stamps = data["timestamp"].tolist()
    for last_seen, length, dates in sampler.seen:
        o = stamps.index(last_seen) + 1
        assert length == min(100, o)
        # the dates the model is told about are exactly the sessions it is scored on
        assert [pd.Timestamp(d) for d in dates] == [s.tz_localize(None) for s in stamps[o:o + 3]]
    assert sampler.seen[-1][0] == stamps[-4]


# -------------------------------------------------------------------- stage
def test_stage_writes_a_scenario_range_with_its_caveats(ctx):
    stage = ForecastStage(lambda c: pytest.fail("TimesFM is disabled"), lambda c: SyntheticSampler())
    stage._gate(ctx)
    result = stage.run(ctx)
    assert result.artifacts == ["forecast_kronos"]

    df = ctx.read_parquet("forecast_kronos", contracts.SCENARIOS)
    assert len(df) == 10 and (df["close_low"] <= df["close_median"]).all()
    assert (df["close_median"] <= df["close_high"]).all()
    assert df["date"].dt.date.tolist()[0] == date(2026, 9, 23)
    report = ctx.read_json("forecast_kronos")
    assert report["kind"] == "scenario_range" and not report["degraded"]
    assert report["caveats"][0].startswith("Scenario range, not a prediction")
    assert report["backtest"]["windows"] == 10
    assert "prediction" not in result.summary.lower().replace("not a prediction", "")


class CorruptingSampler(SyntheticSampler):
    def sample(self, histories, future_dates, n_paths):
        out = super().sample(histories, future_dates, n_paths)
        for paths in out:
            paths[: n_paths // 3, :, CANDLE.index("high")] = 1.0     # a third impossible
        return out


def test_stage_flags_a_degraded_range_but_still_reports_it(ctx):
    ctx.settings = dataclasses.replace(ctx.settings, forecast=dataclasses.replace(
        ctx.settings.forecast, kronos_backtest_windows=0))
    result = ForecastStage(sampler_factory=lambda c: CorruptingSampler()).run(ctx)
    report = ctx.read_json("forecast_kronos")
    assert report["degraded"] and report["caveats"][0].startswith("DEGRADED")
    assert report["backtest"] is None and result.summary.endswith("DEGRADED")


def test_backtest_caveats_name_a_narrow_or_biased_range():
    bt = {"windows": 10, "horizon": 5, "points": 50, "coverage_pct": 64.0,
          "coverage_target_pct": 80.0, "median_bias_pct": -1.8}
    notes = backtest_caveats(bt)
    assert "small sample" in notes[0]
    assert any("too narrow" in n for n in notes) and any("ran low by 1.8%" in n for n in notes)
    fine = backtest_caveats({**bt, "coverage_pct": 78.0, "median_bias_pct": 0.3})
    assert len(fine) == 1


# ------------------------------------------------------------------- config
@pytest.mark.parametrize("kwargs,match", [
    ({"kronos_context": 1024}, "1..512"),
    ({"kronos_paths": 3}, ">= 10"),
    ({"kronos_provider": "chronos"}, "kronos_provider"),
    ({"kronos_max_invalid_pct": 150}, "0..100"),
])
def test_config_refuses_bad_kronos_settings(kwargs, match):
    with pytest.raises(ConfigError, match=match):
        ForecastSettings(**kwargs)


# ------------------------------------------------------------ Kronos adapter
def fake_kronos(monkeypatch, *, columns=("open", "high", "low", "close", "volume", "amount")):
    calls = {"batches": [], "seed": [], "loaded": []}

    class Loadable:
        @classmethod
        def from_pretrained(cls, repo, revision=None):
            calls["loaded"].append((cls.__name__, repo, revision))
            return cls()

    class KronosTokenizer(Loadable):
        pass

    class Kronos(Loadable):
        pass

    class KronosPredictor:
        def __init__(self, model, tokenizer, device=None, max_context=512, clip=5):
            calls["predictor"] = {"device": device, "max_context": max_context}

        def predict_batch(self, df_list, x_timestamp_list, y_timestamp_list, pred_len, T=1.0,
                          top_k=0, top_p=0.9, sample_count=1, verbose=True):
            calls["batches"].append({"n": len(df_list), "sample_count": sample_count, "T": T,
                                     "top_p": top_p, "lens": {len(d) for d in df_list},
                                     "stamp_types": {type(s).__name__ for s in x_timestamp_list}})
            out = []
            for df, y in zip(df_list, y_timestamp_list):
                last = float(df["close"].iloc[-1])
                row = {c: last for c in columns}
                row["high"], row["low"] = last + 1, last - 1
                out.append(pd.DataFrame([row] * pred_len, index=y))
            return out

    vendor = types.ModuleType("pipeline.vendor.kronos")
    vendor.Kronos, vendor.KronosTokenizer, vendor.KronosPredictor = Kronos, KronosTokenizer, KronosPredictor
    monkeypatch.setitem(sys.modules, "pipeline.vendor.kronos", vendor)
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(
        manual_seed=lambda s: calls["seed"].append(s)))
    return calls


def make_sampler(**overrides):
    kwargs = dict(model_revision="abc", tokenizer="tok/repo", tokenizer_revision="def",
                  device="cpu", max_context=512, temperature=1.0, top_p=0.9, batch_size=32, seed=7)
    kwargs.update(overrides)
    return KronosSampler("model/repo", **kwargs)


def test_adapter_samples_each_path_separately_in_chunks(monkeypatch):
    calls = fake_kronos(monkeypatch)
    sampler = make_sampler(batch_size=8)
    assert calls["loaded"] == [("KronosTokenizer", "tok/repo", "def"), ("Kronos", "model/repo", "abc")]
    assert sampler.name == "kronos:model/repo@abc"

    h1, h2 = bars(120), bars(90).assign(close=lambda d: d["close"] + 50)
    dates = next_sessions(date(2026, 9, 22), 4)
    out = sampler.sample([h1, h2], [dates, dates], n_paths=10)

    assert [p.shape for p in out] == [(10, 4, 5), (10, 4, 5)]
    assert [b["n"] for b in calls["batches"]] == [8, 8, 4]
    # sample_count > 1 would average paths inside Kronos: never used
    assert {b["sample_count"] for b in calls["batches"]} == {1}
    # predict_batch needs equal lengths: both trimmed to the shorter history
    assert set().union(*(b["lens"] for b in calls["batches"])) == {90}
    assert {"Series"} == set().union(*(b["stamp_types"] for b in calls["batches"]))
    # paths stay with their own history
    assert out[0][:, 0, CANDLE.index("close")] == pytest.approx(h1["close"].iloc[-1])
    assert out[1][:, 0, CANDLE.index("close")] == pytest.approx(h2["close"].iloc[-1])
    assert calls["seed"] == [7]


def test_adapter_refuses_output_without_the_candle_columns(monkeypatch):
    fake_kronos(monkeypatch, columns=("open", "high", "low", "close"))
    with pytest.raises(ProviderError, match="lacks \\['volume'\\]"):
        make_sampler().sample([bars(80)], [next_sessions(date(2026, 9, 22), 2)], n_paths=10)
