"""Phase 3: the forecast stage, its honesty checks, and the TimesFM adapter.

No PyTorch and no weights here: models are stand-ins, and the TimesFM adapter is
exercised against a fake `timesfm3` module shaped like the installed 3.0.2 API.
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
from pipeline.errors import ConfigError, PipelineHalt, ProviderError
from pipeline.forecasting import (
    SyntheticForecaster,
    TimesFMForecaster,
    backtest,
    check_forecast,
    forecast_series,
    quantile_index,
)
from pipeline.scenarios import SyntheticSampler
from pipeline.session_check import is_trading_day, next_sessions, us_market_holidays
from pipeline.stages import build_registry
from pipeline.stages.forecast import ForecastStage

ROOT = Path(__file__).resolve().parent.parent
QS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]


# ------------------------------------------------------------- trading days
def test_market_holidays_2026_by_rule():
    assert sorted(d.isoformat() for d in us_market_holidays(2026)) == [
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
        "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25"]


def test_observed_holidays_move_to_the_nearest_weekday():
    h = us_market_holidays(2027)
    assert date(2027, 6, 18) in h        # Juneteenth on a Saturday -> Friday
    assert date(2027, 7, 5) in h         # July 4 on a Sunday -> Monday
    assert date(2027, 12, 24) in h       # Christmas on a Saturday -> Friday
    assert date(2027, 3, 26) in h        # Good Friday (Easter 2027-03-28)


def test_saturday_new_year_is_not_moved_back():
    # 2022-01-01 was a Saturday; the market traded on Friday 2021-12-31.
    assert date(2021, 12, 31) not in us_market_holidays(2021)
    assert is_trading_day(date(2021, 12, 31))


def test_next_sessions_skip_weekends_and_holidays():
    # Wednesday before Thanksgiving 2026 -> Fri 27 (half day), then Mon 30.
    assert next_sessions(date(2026, 11, 25), 2) == [date(2026, 11, 27), date(2026, 11, 30)]
    assert not is_trading_day(date(2026, 11, 26))


# ------------------------------------------------------------------- config
def test_prices_are_never_forecast_with_timesfm():
    with pytest.raises(ConfigError, match="never forecast"):
        ForecastSettings(timesfm_series=("volume", "close"))


def test_unknown_series_and_bad_interval_are_rejected():
    with pytest.raises(ConfigError, match="not supported"):
        ForecastSettings(timesfm_series=("open_interest",))
    with pytest.raises(ConfigError, match="around 0.5"):
        ForecastSettings(interval=(0.6, 0.9))


def test_forecast_section_in_config_yaml_loads(tmp_path):
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    f = load_settings(tmp_path / "config.yaml", root=tmp_path).forecast
    assert f.timesfm_checkpoint == "google/timesfm-3.0-pytorch" and f.timesfm_series == ("volume",)


# ------------------------------------------------------------------- checks
def test_quantile_index_refuses_a_missing_quantile():
    assert quantile_index(QS, 0.9) == 8
    with pytest.raises(ProviderError, match="no 0.05 quantile"):
        quantile_index(QS, 0.05)


@pytest.mark.parametrize("mutate,match", [
    (lambda q: q.__setitem__((0, 0), np.nan), "NaN"),
    (lambda q: q.__setitem__((0, 8), 0.0), "increasing order"),
    (lambda q: q.__isub__(200.0), "negative"),
    (lambda q: q.__imul__(50.0), "implausible"),
])
def test_impossible_forecasts_fail_loudly(mutate, match):
    q = np.tile(np.linspace(90, 110, 9), (5, 1))
    mutate(q)
    with pytest.raises(ProviderError, match=match):
        check_forecast(q, 100.0, nonnegative=True, context="t")


# ------------------------------------------------------------------ backtest
class Oracle:
    """Knows the whole series and returns the true future, with a ±1% band —
    a model whose backtest must come out perfect."""

    name = "oracle"
    quantiles = QS

    def __init__(self, values):
        self.values = np.asarray(values, dtype=float)

    def forecast(self, contexts, horizon):
        out = []
        for c in contexts:
            origin = len(c)                          # contexts are never truncated here
            truth = self.values[origin:origin + horizon]
            if len(truth) < horizon:
                truth = np.full(horizon, self.values[-1])
            out.append(np.stack([truth * (1 + 0.02 * (q - 0.5)) for q in QS], axis=1))
        return out


def wave(n=200):
    return 1000 * (1 + 0.3 * np.sin(np.arange(n) / 1.3))


def test_backtest_of_a_perfect_model_is_perfect():
    values = wave()
    bt = backtest(values, Oracle(values), windows=30, horizon=5, context_length=1000,
                  interval=(0.1, 0.9), series="volume")
    assert bt["windows"] == 30 and bt["points"] == 150
    assert bt["mape_pct"] == 0 and bt["coverage_pct"] == 100 and bt["skill_vs_naive_pct"] == 100
    assert bt["naive_mape_pct"] > 0 and len(bt["mape_by_step_pct"]) == 5


def test_backtest_of_the_naive_model_has_no_skill():
    values = wave()
    bt = backtest(values, SyntheticForecaster(), windows=30, horizon=5, context_length=1000,
                  interval=(0.1, 0.9), series="volume")
    assert bt["mape_pct"] == bt["naive_mape_pct"] and bt["skill_vs_naive_pct"] == 0


def test_backtest_never_uses_the_future():
    values = wave()
    seen = []

    class Spy(SyntheticForecaster):
        def forecast(self, contexts, horizon):
            seen.extend(len(c) for c in contexts)
            return super().forecast(contexts, horizon)

    backtest(values, Spy(), windows=10, horizon=5, context_length=1000, interval=(0.1, 0.9),
             series="volume")
    # origins run up to n - horizon, so the last window's actuals are the last 5 values
    assert max(seen) == len(values) - 5 and min(seen) == len(values) - 5 - 9


def test_too_short_history_is_an_error():
    with pytest.raises(ProviderError, match="too short"):
        backtest(wave(40), SyntheticForecaster(), windows=10, horizon=5, context_length=512,
                 interval=(0.1, 0.9), series="volume")


def test_forecast_uses_only_the_last_context_length_points():
    seen = []

    class Spy(SyntheticForecaster):
        def forecast(self, contexts, horizon):
            seen.append(len(contexts[0]))
            return super().forecast(contexts, horizon)

    bands = forecast_series(wave(), Spy(), horizon=7, context_length=64, interval=(0.1, 0.9),
                            series="volume")
    assert seen == [64] and bands.shape == (7, 3)
    assert np.all(bands[:, 0] <= bands[:, 1]) and np.all(bands[:, 1] <= bands[:, 2])


# --------------------------------------------------------------------- stage
@pytest.fixture
def ctx(tmp_path):
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    settings = load_settings(tmp_path / "config.yaml", root=tmp_path)
    c = make_run_context(settings, "TEST", "2026-09-22")
    days = [d for d in pd.bdate_range("2026-01-02", "2026-09-22") if is_trading_day(d.date())]
    n = len(days)
    price = pd.Series(np.linspace(100, 120, n))
    c.write_parquet("data_ohlcv", pd.DataFrame({
        "timestamp": pd.DatetimeIndex(days).tz_localize("UTC"), "symbol": "TEST",
        "open": price, "high": price + 1, "low": price - 1, "close": price,
        "volume": wave(n)}))
    return c


def test_forecast_stage_is_behind_the_validation_gate(ctx):
    with pytest.raises(PipelineHalt, match="has not run"):
        build_registry()["forecast"]._gate(ctx)


def test_forecast_stage_writes_band_and_backtest(ctx):
    ctx.write_json("validation", {"status": "pass", "checks": []})
    stage = ForecastStage(lambda c: SyntheticForecaster(), lambda c: SyntheticSampler())
    stage._gate(ctx)
    result = stage.run(ctx)

    df = ctx.read_parquet("forecast_timesfm", contracts.FORECAST)
    assert len(df) == 10 and df["series"].unique().tolist() == ["volume"]
    dates = df["date"].dt.date.tolist()
    assert dates[0] == date(2026, 9, 23) and all(is_trading_day(d) for d in dates)
    report = ctx.read_json("forecast_timesfm")
    bt = report["series"]["volume"]["backtest"]
    assert report["model"] == "synthetic" and bt["windows"] == 60 and bt["coverage_target_pct"] == 80
    assert result.artifacts == ["forecast_timesfm", "forecast_kronos"]
    assert "Kronos scenario range" in result.summary


def test_disabled_forecast_is_skipped(ctx):
    ctx.write_json("validation", {"status": "pass", "checks": []})
    settings = dataclasses.replace(ctx.settings, forecast=ForecastSettings(timesfm_enabled=False,
                                                                           kronos_enabled=False))
    ctx.settings = settings
    stage = ForecastStage(lambda c: pytest.fail("model loaded"), lambda c: pytest.fail("model loaded"))
    assert stage.run(ctx).status == "skipped"


# ----------------------------------------------------------- TimesFM adapter
def fake_timesfm3(monkeypatch, shape_ok=True):
    calls = {}

    class Output:
        def __init__(self, q):
            self.quantiles = q
            self.forecast = q[:, q.shape[1] // 2]

    class FakeForecaster:
        def __init__(self):
            self.config = types.SimpleNamespace(quantiles=QS)

        @classmethod
        def from_pretrained(cls, path, device=None, **kwargs):
            calls["from_pretrained"] = (path, device, kwargs)
            return cls()

        def predict_batch(self, contexts, horizon, **kwargs):
            calls["predict_batch"] = ([c.dtype for c in contexts], horizon, kwargs)
            for c in contexts:
                width = len(QS) if shape_ok else 3
                yield Output(np.tile(np.linspace(0.9, 1.1, width) * float(c[-1]), (horizon, 1)))

    module = types.ModuleType("timesfm3")
    module.TimesFM3Forecaster = FakeForecaster
    monkeypatch.setitem(sys.modules, "timesfm3", module)
    return calls


def test_timesfm_adapter_uses_the_installed_api(monkeypatch):
    calls = fake_timesfm3(monkeypatch)
    model = TimesFMForecaster("google/timesfm-3.0-pytorch", revision="abc123", device="cpu",
                              batch_size=16)
    out = model.forecast([np.arange(1, 50, dtype=float)], 6)
    assert calls["from_pretrained"] == ("google/timesfm-3.0-pytorch", "cpu",
                                        {"revision": "abc123", "per_core_batch_size": 16})
    dtypes, horizon, kwargs = calls["predict_batch"]
    assert dtypes == [np.float32] and horizon == 6
    assert kwargs == {"return_quantiles": True, "make_positive": True}
    assert out[0].shape == (6, 9) and model.name == "timesfm:google/timesfm-3.0-pytorch@abc123"


def test_timesfm_adapter_refuses_unexpected_shapes(monkeypatch):
    fake_timesfm3(monkeypatch, shape_ok=False)
    model = TimesFMForecaster("x", revision=None, device="cpu", batch_size=1)
    with pytest.raises(ProviderError, match="shape"):
        model.forecast([np.ones(10)], 4)


def test_missing_timesfm_says_how_to_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "timesfm3", None)      # import fails
    with pytest.raises(ProviderError, match="pip install -r requirements.txt"):
        TimesFMForecaster("x", revision=None, device="cpu", batch_size=1)
