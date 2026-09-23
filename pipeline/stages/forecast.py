"""Stage 5 — forecast.

Two halves, each switched on in config:

- TimesFM (phase 3): a volume forecast with a quantile band, plus a rolling
  backtest on the same history so the band's reliability is on record next to it.
- Kronos (phase 5): many sampled candle paths, reported only as a scenario range
  of closes (never a single price, never called a prediction). Invalid generated
  candles are dropped and counted; above `kronos_max_invalid_pct` the result is
  flagged degraded. See pipeline/scenarios.py.

Reads only `data_ohlcv.parquet` of a run whose validation passed — the gate
runs before this stage starts.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

import numpy as np
import pandas as pd

from .. import contracts, scenarios
from ..cache import RunContext
from ..errors import ProviderError
from ..forecasting import (
    QuantileForecaster,
    SyntheticForecaster,
    TimesFMForecaster,
    backtest,
    forecast_series,
    to_frame,
)
from ..session_check import next_sessions
from .base import Stage, StageResult

log = logging.getLogger(__name__)

ARTIFACT = "forecast_timesfm"
KRONOS_ARTIFACT = "forecast_kronos"

SCENARIO_CAVEATS = [
    "Scenario range, not a prediction: the spread of closes across paths the model "
    "sampled. A single stock's price path cannot be forecast reliably.",
    "Invalid generated candles were dropped, never repaired; their share is reported.",
    "Forecast dates are scheduled US trading days; unscheduled closures are not known in advance.",
]


def default_forecaster(ctx: RunContext) -> QuantileForecaster:
    f = ctx.settings.forecast
    if f.timesfm_provider == "synthetic":
        return SyntheticForecaster()
    return TimesFMForecaster(f.timesfm_checkpoint, revision=f.timesfm_revision,
                             device=f.timesfm_device, batch_size=f.timesfm_batch_size)


def default_sampler(ctx: RunContext) -> scenarios.PathSampler:
    f = ctx.settings.forecast
    if f.kronos_provider == "synthetic":
        return scenarios.SyntheticSampler(seed=f.kronos_seed)
    return scenarios.KronosSampler(
        f.kronos_model, model_revision=f.kronos_model_revision, tokenizer=f.kronos_tokenizer,
        tokenizer_revision=f.kronos_tokenizer_revision, device=f.kronos_device,
        max_context=f.kronos_context, temperature=f.kronos_temperature, top_p=f.kronos_top_p,
        batch_size=f.kronos_batch_size, seed=f.kronos_seed)


def series_values(ohlcv: pd.DataFrame, series: str) -> tuple[np.ndarray, pd.Series]:
    if series not in ohlcv.columns:
        raise ProviderError(f"data_ohlcv has no '{series}' column")
    frame = ohlcv[["timestamp", series]].dropna()
    return frame[series].to_numpy(dtype=float), frame["timestamp"]


def backtest_caveats(bt: dict) -> list[str]:
    """What the Kronos backtest says about trusting the range, in words the report can show."""
    notes = [f"Backtest: {bt['windows']} windows x {bt['horizon']} sessions = {bt['points']} "
             "points — a small sample; its numbers are indicative only."]
    if bt["coverage_pct"] < bt["coverage_target_pct"] - 10:
        notes.append(f"In the backtest the range held the real close {bt['coverage_pct']}% of the "
                     f"time against a {bt['coverage_target_pct']}% target: it has been too narrow.")
    if abs(bt["median_bias_pct"]) >= 1:
        side = "low" if bt["median_bias_pct"] < 0 else "high"
        notes.append(f"In the backtest the middle of the range ran {side} by "
                     f"{abs(bt['median_bias_pct'])}% on average.")
    return notes


def candle_history(ohlcv: pd.DataFrame) -> pd.DataFrame:
    """Bars Kronos may see: complete candles only, oldest first."""
    bars = ohlcv[["timestamp", *scenarios.CANDLE]].dropna().sort_values("timestamp")
    if len(bars) != len(ohlcv):
        log.info("Kronos: %d bar(s) with a missing field left out of its history",
                 len(ohlcv) - len(bars))
    return bars.reset_index(drop=True)


class ForecastStage(Stage):
    name = "forecast"

    def __init__(self, forecaster_factory: Callable[[RunContext], QuantileForecaster] = default_forecaster,
                 sampler_factory: Callable[[RunContext], scenarios.PathSampler] = default_sampler):
        self.forecaster_factory = forecaster_factory
        self.sampler_factory = sampler_factory

    def run(self, ctx: RunContext) -> StageResult:
        f = ctx.settings.forecast
        if not (f.timesfm_enabled or f.kronos_enabled):
            return StageResult(stage=self.name, status="skipped",
                               summary="TimesFM and Kronos are both disabled in config")

        ohlcv = ctx.read_parquet("data_ohlcv", contracts.OHLCV)
        artifacts, summaries, details = [], [], {}
        if f.timesfm_enabled:
            summary, details["forecast"] = self._timesfm(ctx, ohlcv)
            artifacts.append(ARTIFACT)
            summaries.append(summary)
        if f.kronos_enabled:
            summary, details["scenarios"] = self._kronos(ctx, ohlcv)
            artifacts.append(KRONOS_ARTIFACT)
            summaries.append(summary)
        return StageResult(stage=self.name, status="ok", summary="; ".join(summaries),
                           artifacts=artifacts, details=details)

    # ------------------------------------------------------------- TimesFM
    def _timesfm(self, ctx: RunContext, ohlcv: pd.DataFrame) -> tuple[str, dict]:
        f = ctx.settings.forecast
        started = time.monotonic()
        model = self.forecaster_factory(ctx)

        frames, report = [], {
            "model": model.name,
            "device": f.timesfm_device if f.timesfm_provider == "timesfm" else None,
            "interval": list(f.interval),
            "horizon": f.horizon,
            "context_length": f.context_length,
            "series": {},
            "caveats": [
                "Forecast dates are scheduled US trading days; unscheduled closures are not known in advance.",
                "The band is the model's own quantiles; the backtest shows how often such a band held.",
            ],
        }
        for series in f.timesfm_series:
            values, stamps = series_values(ohlcv, series)
            last_day = pd.Timestamp(stamps.iloc[-1]).date()
            bands = forecast_series(values, model, horizon=f.horizon, context_length=f.context_length,
                                    interval=f.interval, series=series)
            dates = next_sessions(last_day, f.horizon)
            frames.append(to_frame(bands, dates, series=series, model=model.name))
            bt = backtest(values, model, windows=f.backtest_windows, horizon=f.backtest_horizon,
                          context_length=f.context_length, interval=f.interval, series=series)
            report["series"][series] = {
                "observations": int(len(values)),
                "context_used": int(min(len(values), f.context_length)),
                "last_date": last_day.isoformat(),
                "last_value": float(values[-1]),
                "mean_last_20": float(np.mean(values[-20:])),
                "backtest": bt,
            }
            log.info("%s: next %d sessions median %.4g..%.4g (band %.4g..%.4g)", series, f.horizon,
                     bands[:, 1].min(), bands[:, 1].max(), bands[:, 0].min(), bands[:, 2].max())
            log.info("%s backtest (%d windows x %d steps): error %.1f%% vs naive %.1f%% "
                     "(skill %s%%), band held %.0f%% of the time (target %.0f%%)", series,
                     bt["windows"], bt["horizon"], bt["mape_pct"], bt["naive_mape_pct"],
                     bt["skill_vs_naive_pct"], bt["coverage_pct"], bt["coverage_target_pct"])
            if bt["skill_vs_naive_pct"] is not None and bt["skill_vs_naive_pct"] <= 0:
                log.warning("%s: the model did not beat the naive forecast in the backtest — "
                            "treat its band with suspicion", series)

        report["seconds"] = round(time.monotonic() - started, 1)
        ctx.write_parquet(ARTIFACT, pd.concat(frames, ignore_index=True), contracts.FORECAST)
        ctx.write_json(ARTIFACT, report)

        first = report["series"][f.timesfm_series[0]]["backtest"]
        summary = (f"TimesFM {', '.join(f.timesfm_series)}: {f.horizon} sessions ahead; backtest "
                   f"error {first['mape_pct']}% (naive {first['naive_mape_pct']}%), band held "
                   f"{first['coverage_pct']}% (target {first['coverage_target_pct']}%)")
        return summary, {k: v for k, v in report.items() if k != "caveats"}

    # -------------------------------------------------------------- Kronos
    def _kronos(self, ctx: RunContext, ohlcv: pd.DataFrame) -> tuple[str, dict]:
        f = ctx.settings.forecast
        bars = candle_history(ohlcv)
        started = time.monotonic()
        sampler = self.sampler_factory(ctx)
        loaded = time.monotonic() - started

        history = bars.iloc[-f.kronos_context:]
        last_day = pd.Timestamp(history["timestamp"].iloc[-1]).date()
        last_close = float(history["close"].iloc[-1])
        dates = next_sessions(last_day, f.horizon)
        started = time.monotonic()
        paths = sampler.sample([history], [dates], f.kronos_paths)[0]
        sampled = time.monotonic() - started
        result = scenarios.summarize(paths, last_close=last_close, interval=f.interval,
                                     max_invalid_pct=f.kronos_max_invalid_pct, context=sampler.name)

        report = {
            "model": sampler.name,
            "tokenizer": f.kronos_tokenizer if f.kronos_provider == "kronos" else None,
            "device": f.kronos_device if f.kronos_provider == "kronos" else None,
            "kind": "scenario_range",
            "interval": list(f.interval),
            "horizon": f.horizon,
            "context_used": int(len(history)),
            "last_date": last_day.isoformat(),
            "last_close": last_close,
            "sampling": {"temperature": f.kronos_temperature, "top_p": f.kronos_top_p,
                         "seed": f.kronos_seed},
            **{k: v for k, v in result.items() if k != "steps"},
            "backtest": None,
            "caveats": list(SCENARIO_CAVEATS),
        }
        if result["degraded"]:
            report["caveats"].insert(0, f"DEGRADED: {result['invalid_pct']}% of generated candles "
                                        f"were invalid (limit {f.kronos_max_invalid_pct}%).")
            log.warning("Kronos: %.1f%% of generated candles were invalid (limit %.0f%%) — "
                        "the scenario range is flagged degraded", result["invalid_pct"],
                        f.kronos_max_invalid_pct)
        final = result["steps"][-1]
        log.info("Kronos: %d paths x %d sessions; %d invalid candle(s) dropped (%.1f%%); "
                 "close range after %d sessions %.4g..%.4g (median %.4g) from %.4g", f.kronos_paths,
                 f.horizon, result["invalid_candles"], result["invalid_pct"], f.horizon,
                 final["close_low"], final["close_high"], final["close_median"], last_close)

        backtest_seconds = 0.0
        if f.kronos_backtest_windows:
            started = time.monotonic()
            bt = scenarios.backtest(
                bars, sampler, windows=f.kronos_backtest_windows, horizon=f.backtest_horizon,
                n_paths=f.kronos_backtest_paths, context_length=f.kronos_context,
                interval=f.interval, max_invalid_pct=f.kronos_max_invalid_pct,
                sessions_after=next_sessions)
            backtest_seconds = time.monotonic() - started
            report["backtest"] = bt
            report["caveats"].extend(backtest_caveats(bt))
            log.info("Kronos backtest (%d windows x %d steps, %d paths): median error %.2f%% vs "
                     "'no change' %.2f%% (skill %s%%), bias %+.2f%%, range held %.0f%% (target "
                     "%.0f%%), %.1f%% invalid candles", bt["windows"], bt["horizon"],
                     bt["paths_per_window"], bt["median_error_pct"], bt["naive_error_pct"],
                     bt["skill_vs_naive_pct"], bt["median_bias_pct"], bt["coverage_pct"],
                     bt["coverage_target_pct"], bt["invalid_pct"])

        report["seconds"] = {"load": round(loaded, 1), "sample": round(sampled, 1),
                             "backtest": round(backtest_seconds, 1)}
        ctx.write_parquet(KRONOS_ARTIFACT, scenarios.to_frame(result, dates, model=sampler.name),
                          contracts.SCENARIOS)
        ctx.write_json(KRONOS_ARTIFACT, report)

        summary = (f"Kronos scenario range ({f.kronos_paths} paths): close after {f.horizon} "
                   f"sessions {final['close_low']:.2f}..{final['close_high']:.2f}, "
                   f"{result['invalid_pct']}% invalid candles"
                   + (" — DEGRADED" if result["degraded"] else ""))
        return summary, {k: v for k, v in report.items() if k != "caveats"}
