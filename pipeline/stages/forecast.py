"""Stage 5 — forecast.

Phase 3 builds the TimesFM half: a volume forecast with a quantile band, plus a
rolling backtest on the same history so the band's reliability is on record
next to it. Kronos (price scenarios) joins in phase 5.

Reads only `data_ohlcv.parquet` of a run whose validation passed — the gate
runs before this stage starts.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

import numpy as np
import pandas as pd

from .. import contracts
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


def default_forecaster(ctx: RunContext) -> QuantileForecaster:
    f = ctx.settings.forecast
    if f.timesfm_provider == "synthetic":
        return SyntheticForecaster()
    return TimesFMForecaster(f.timesfm_checkpoint, revision=f.timesfm_revision,
                             device=f.timesfm_device, batch_size=f.timesfm_batch_size)


def series_values(ohlcv: pd.DataFrame, series: str) -> tuple[np.ndarray, pd.Series]:
    if series not in ohlcv.columns:
        raise ProviderError(f"data_ohlcv has no '{series}' column")
    frame = ohlcv[["timestamp", series]].dropna()
    return frame[series].to_numpy(dtype=float), frame["timestamp"]


class ForecastStage(Stage):
    name = "forecast"

    def __init__(self, forecaster_factory: Callable[[RunContext], QuantileForecaster] = default_forecaster):
        self.forecaster_factory = forecaster_factory

    def run(self, ctx: RunContext) -> StageResult:
        f = ctx.settings.forecast
        if not f.timesfm_enabled:
            return StageResult(stage=self.name, status="skipped",
                               summary="TimesFM disabled in config; Kronos arrives in phase 5")

        ohlcv = ctx.read_parquet("data_ohlcv", contracts.OHLCV)
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
        return StageResult(
            stage=self.name, status="ok",
            summary=(f"TimesFM {', '.join(f.timesfm_series)}: {f.horizon} sessions ahead; backtest "
                     f"error {first['mape_pct']}% (naive {first['naive_mape_pct']}%), band held "
                     f"{first['coverage_pct']}% (target {first['coverage_target_pct']}%). "
                     "Kronos arrives in phase 5"),
            artifacts=[ARTIFACT],
            details={"forecast": {k: v for k, v in report.items() if k != "caveats"}},
        )
