"""Quantile forecasting and its honesty checks (phase 3: TimesFM on volume).

The stage never trusts a forecast on its own say-so:

- every output is checked (finite, ordered quantiles, non-negative where the
  series is, not wildly off the recent level) — a bad forecast fails loudly;
- a rolling backtest runs the same model on the series' own history and
  reports how wrong it was, whether the interval covered what happened, and
  whether it beat the naive "same as yesterday" forecast. Those numbers are
  what make the forecast range mean something.

TimesFM 3.0.2 API, read from the installed source (not the README):
    from timesfm3 import TimesFM3Forecaster
    m = TimesFM3Forecaster.from_pretrained("google/timesfm-3.0-pytorch", device=...,
                                           revision=..., per_core_batch_size=...)
    m.config.quantiles                      -> [0.1, 0.2, ..., 0.9]
    m.predict_batch(contexts, horizon, return_quantiles=True, make_positive=True)
        yields ForecastOutput(forecast=(h,), quantiles=(h, n_quantiles)) per input
"""
from __future__ import annotations

import logging
import math
from typing import Protocol, Sequence

import numpy as np
import pandas as pd

from .errors import ProviderError

log = logging.getLogger(__name__)

MIN_CONTEXT = 64            # shortest history a backtest window may use
PLAUSIBLE_FACTOR = 10.0     # a median outside [recent/10, recent*10] is a broken forecast


class QuantileForecaster(Protocol):
    name: str
    quantiles: list[float]

    def forecast(self, contexts: Sequence[np.ndarray], horizon: int) -> list[np.ndarray]:
        """One (horizon, len(quantiles)) array per context."""


# -------------------------------------------------------------------- models
class TimesFMForecaster:
    """The real model. Imported lazily so the rest of the pipeline (and the
    tests) never need PyTorch."""

    def __init__(self, checkpoint: str, *, revision: str | None, device: str, batch_size: int):
        try:
            from timesfm3 import TimesFM3Forecaster
        except ImportError as exc:
            raise ProviderError(
                f"TimesFM is not installed ({exc}). Run: pip install -r requirements.txt"
            ) from exc
        log.info("loading TimesFM %s%s on %s (first run downloads the weights)", checkpoint,
                 f"@{revision}" if revision else "", device)
        try:
            self._model = TimesFM3Forecaster.from_pretrained(
                checkpoint, device=device, revision=revision, per_core_batch_size=batch_size)
        except Exception as exc:  # noqa: BLE001 — download/load failures all mean "no model"
            raise ProviderError(f"could not load TimesFM checkpoint '{checkpoint}': "
                                f"{type(exc).__name__}: {exc}") from exc
        self.quantiles = [float(q) for q in self._model.config.quantiles]
        self.name = f"timesfm:{checkpoint}" + (f"@{revision}" if revision else "")

    def forecast(self, contexts: Sequence[np.ndarray], horizon: int) -> list[np.ndarray]:
        outputs = list(self._model.predict_batch(
            [np.asarray(c, dtype=np.float32) for c in contexts], horizon=horizon,
            return_quantiles=True, make_positive=True))
        if len(outputs) != len(contexts):
            raise ProviderError(f"TimesFM returned {len(outputs)} forecasts for {len(contexts)} inputs")
        result = []
        for out in outputs:
            q = np.asarray(out.quantiles, dtype=float)
            if q.shape != (horizon, len(self.quantiles)):
                raise ProviderError(f"TimesFM quantiles have shape {q.shape}, expected "
                                    f"({horizon}, {len(self.quantiles)})")
            result.append(q)
        return result


class SyntheticForecaster:
    """Offline stand-in for --selftest and tests: the last value, with a fixed
    ±spread band. Never selected unless config asks for it."""

    name = "synthetic"

    def __init__(self, spread: float = 0.2):
        self.quantiles = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
        self.spread = spread

    def forecast(self, contexts: Sequence[np.ndarray], horizon: int) -> list[np.ndarray]:
        out = []
        for c in contexts:
            last = float(np.asarray(c)[-1])
            row = [last * (1 + self.spread * (q - 0.5) / 0.4) for q in self.quantiles]
            out.append(np.tile(row, (horizon, 1)))
        return out


# ------------------------------------------------------------------- helpers
def quantile_index(quantiles: Sequence[float], q: float) -> int:
    for i, value in enumerate(quantiles):
        if math.isclose(value, q, abs_tol=1e-6):
            return i
    raise ProviderError(f"model has no {q} quantile (it offers {list(quantiles)})")


def check_forecast(q: np.ndarray, recent_level: float, *, nonnegative: bool, context: str) -> None:
    """Refuse forecasts that cannot be right, instead of reporting them."""
    if not np.all(np.isfinite(q)):
        raise ProviderError(f"{context}: forecast contains NaN or infinity")
    if np.any(np.diff(q, axis=1) < -1e-9):
        raise ProviderError(f"{context}: quantiles are not in increasing order")
    if nonnegative and np.any(q < 0):
        raise ProviderError(f"{context}: negative values forecast for a non-negative series")
    median = q[:, q.shape[1] // 2]
    if recent_level > 0 and (np.any(median > recent_level * PLAUSIBLE_FACTOR)
                             or np.any(median < recent_level / PLAUSIBLE_FACTOR)):
        raise ProviderError(f"{context}: median forecast {median.min():.4g}..{median.max():.4g} is "
                            f"implausible against the recent level {recent_level:.4g}")


def forecast_series(values: np.ndarray, model: QuantileForecaster, *, horizon: int,
                    context_length: int, interval: tuple[float, float], series: str) -> np.ndarray:
    """(horizon, 3) array of low / median / high for the next `horizon` steps."""
    lo, mid, hi = (quantile_index(model.quantiles, q) for q in (interval[0], 0.5, interval[1]))
    context = np.asarray(values[-context_length:], dtype=float)
    q = model.forecast([context], horizon)[0]
    check_forecast(q, float(np.mean(context[-20:])), nonnegative=bool(np.all(context >= 0)),
                   context=f"{model.name} {series}")
    return q[:, [lo, mid, hi]]


def backtest(values: np.ndarray, model: QuantileForecaster, *, windows: int, horizon: int,
             context_length: int, interval: tuple[float, float], series: str) -> dict:
    """Rolling-origin backtest over the last `windows` origins, in one batch.

    For each origin o the model sees values[:o] (at most `context_length` of
    them) and forecasts values[o:o+horizon]. Reports the median's error, the
    naive "same as the last value" error on the same points, and how often the
    interval contained what actually happened.
    """
    values = np.asarray(values, dtype=float)
    n = len(values)
    origins = [o for o in range(n - horizon - windows + 1, n - horizon + 1) if o >= MIN_CONTEXT]
    if not origins:
        raise ProviderError(f"{series}: {n} observations is too short for a backtest "
                            f"(need at least {MIN_CONTEXT + horizon})")
    lo, mid, hi = (quantile_index(model.quantiles, q) for q in (interval[0], 0.5, interval[1]))
    contexts = [values[max(0, o - context_length):o] for o in origins]
    forecasts = model.forecast(contexts, horizon)

    abs_pct, naive_pct, covered = [], [], []
    per_step = np.zeros(horizon)
    for o, q in zip(origins, forecasts):
        check_forecast(q, float(np.mean(values[max(0, o - 20):o])),
                       nonnegative=bool(np.all(values[:o] >= 0)), context=f"{model.name} {series} backtest")
        actual = values[o:o + horizon]
        if np.any(actual == 0):
            raise ProviderError(f"{series}: zero actual value in the backtest — percentage "
                                "errors are undefined")
        err = np.abs(q[:, mid] - actual) / np.abs(actual)
        abs_pct.extend(err)
        per_step += err
        naive_pct.extend(np.abs(values[o - 1] - actual) / np.abs(actual))
        covered.extend((actual >= q[:, lo]) & (actual <= q[:, hi]))

    mape = 100 * float(np.mean(abs_pct))
    naive = 100 * float(np.mean(naive_pct))
    return {
        "windows": len(origins),
        "horizon": horizon,
        "points": len(abs_pct),
        "mape_pct": round(mape, 2),
        "naive_mape_pct": round(naive, 2),
        "skill_vs_naive_pct": round(100 * (1 - mape / naive), 1) if naive > 0 else None,
        "coverage_pct": round(100 * float(np.mean(covered)), 1),
        "coverage_target_pct": round(100 * (interval[1] - interval[0]), 1),
        "mape_by_step_pct": [round(100 * v / len(origins), 2) for v in per_step],
        "first_origin_index": origins[0],
    }


def to_frame(bands: np.ndarray, dates: Sequence, *, series: str, model: str) -> pd.DataFrame:
    return pd.DataFrame({
        "series": series,
        "step": np.arange(1, len(bands) + 1),
        "date": pd.to_datetime([pd.Timestamp(d) for d in dates]).tz_localize("UTC"),
        "q_low": bands[:, 0],
        "median": bands[:, 1],
        "q_high": bands[:, 2],
        "model": model,
    })
