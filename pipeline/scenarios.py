"""Price scenario ranges from Kronos (phase 5), and the checks that keep them honest.

Kronos generates whole candles (open, high, low, close, volume) one session at a
time by sampling, so every run of it is one possible path, not a prediction.
The pipeline samples many paths and reports the spread of their closes as a
scenario range. It never reports a single path, and it never calls the
median a forecast.

Checks:
- every generated candle goes through the same bar rules as real data
  (`contracts.ohlcv_problem_masks`) plus a finiteness check; an invalid candle is
  dropped and counted, never repaired;
- more than `max_invalid_pct` invalid candles marks the result degraded;
- a step left with fewer than MIN_VALID_PER_STEP valid candles fails the stage;
- a rolling backtest reports how often such a range held the real close and how
  far its median was from it, next to the naive "no change" error.

Kronos API, read from the vendored source (pipeline/vendor/kronos/kronos.py):
    KronosTokenizer.from_pretrained(repo, revision=...)   # huggingface_hub mixin, eval() mode
    Kronos.from_pretrained(repo, revision=...)
    KronosPredictor(model, tokenizer, device=..., max_context=512, clip=5)
    .predict_batch(df_list, x_timestamp_list, y_timestamp_list, pred_len, T, top_k, top_p,
                   sample_count, verbose) -> list of DataFrames
        columns open high low close volume amount; every history must have the
        same length; timestamps are pandas Series (it reads .dt.minute etc.).
    With sample_count > 1 the samples are AVERAGED inside auto_regressive_inference,
    which gives one smoothed path instead of several. So each path here is its own
    batch row with sample_count=1.
"""
from __future__ import annotations

import logging
from typing import Protocol, Sequence

import numpy as np
import pandas as pd

from .contracts import ohlcv_problem_masks
from .errors import ProviderError

log = logging.getLogger(__name__)

CANDLE = ["open", "high", "low", "close", "volume"]
CLOSE = CANDLE.index("close")
MIN_VALID_PER_STEP = 5
KRONOS_MAX_CONTEXT = 512        # Kronos-small / -base were trained with 512


class PathSampler(Protocol):
    name: str

    def sample(self, histories: Sequence[pd.DataFrame], future_dates: Sequence[Sequence],
               n_paths: int) -> list[np.ndarray]:
        """One (n_paths, horizon, 5) array per history, columns as CANDLE.

        Each history has a `timestamp` column and the CANDLE columns, oldest first.
        """


# -------------------------------------------------------------------- models
def _day_stamps(values) -> pd.Series:
    """Session dates as tz-naive midnight timestamps. Daily bars carry no time of
    day, so history and future get the same (0, 0) minute/hour features."""
    stamps = pd.to_datetime(pd.Series(list(values)))
    if stamps.dt.tz is not None:
        stamps = stamps.dt.tz_localize(None)
    return stamps.dt.normalize().reset_index(drop=True)


class KronosSampler:
    """The real model, imported lazily so tests and the rest of the pipeline never
    need PyTorch."""

    def __init__(self, model: str, *, model_revision: str | None, tokenizer: str,
                 tokenizer_revision: str | None, device: str, max_context: int,
                 temperature: float, top_p: float, batch_size: int, seed: int):
        try:
            import torch

            from .vendor.kronos import Kronos, KronosPredictor, KronosTokenizer
        except ImportError as exc:
            raise ProviderError(
                f"Kronos needs PyTorch and einops ({exc}). Run: pip install -r requirements.txt"
            ) from exc
        log.info("loading Kronos %s%s + %s%s on %s (first run downloads the weights)", model,
                 f"@{model_revision}" if model_revision else "", tokenizer,
                 f"@{tokenizer_revision}" if tokenizer_revision else "", device)
        try:
            tok = KronosTokenizer.from_pretrained(tokenizer, revision=tokenizer_revision)
            net = Kronos.from_pretrained(model, revision=model_revision)
        except Exception as exc:  # noqa: BLE001 — download/load failures all mean "no model"
            raise ProviderError(f"could not load Kronos ('{model}', '{tokenizer}'): "
                                f"{type(exc).__name__}: {exc}") from exc
        self._torch = torch
        self._predictor = KronosPredictor(net, tok, device=device, max_context=max_context)
        self.max_context = max_context
        self.temperature = temperature
        self.top_p = top_p
        self.batch_size = batch_size
        self.seed = seed
        self.name = f"kronos:{model}" + (f"@{model_revision}" if model_revision else "")

    def sample(self, histories: Sequence[pd.DataFrame], future_dates: Sequence[Sequence],
               n_paths: int) -> list[np.ndarray]:
        horizons = {len(d) for d in future_dates}
        if len(horizons) != 1:
            raise ProviderError(f"Kronos: every history needs the same horizon, got {sorted(horizons)}")
        horizon = horizons.pop()
        # predict_batch requires equal history lengths: trim all to the shortest.
        length = min(min(len(h) for h in histories), self.max_context)
        rows = []
        for h, dates in zip(histories, future_dates):
            h = h.iloc[-length:]
            x = h[CANDLE].astype(float).reset_index(drop=True)
            rows.extend([(x, _day_stamps(h["timestamp"]), _day_stamps(dates))] * n_paths)

        self._torch.manual_seed(self.seed)        # same inputs -> same paths
        out: list[np.ndarray] = []
        for start in range(0, len(rows), self.batch_size):
            chunk = rows[start:start + self.batch_size]
            frames = self._predictor.predict_batch(
                [r[0] for r in chunk], [r[1] for r in chunk], [r[2] for r in chunk],
                pred_len=horizon, T=self.temperature, top_k=0, top_p=self.top_p,
                sample_count=1, verbose=False)
            if len(frames) != len(chunk):
                raise ProviderError(f"Kronos returned {len(frames)} paths for {len(chunk)} inputs")
            for frame in frames:
                missing = [c for c in CANDLE if c not in frame.columns]
                if missing:
                    raise ProviderError(f"Kronos output lacks {missing}; has {list(frame.columns)}")
                out.append(frame[CANDLE].to_numpy(dtype=float))
        paths = np.stack(out).reshape(len(histories), n_paths, horizon, len(CANDLE))
        return list(paths)


class SyntheticSampler:
    """Offline stand-in for --selftest and tests: a lognormal random walk from the
    last close with well-formed candles. Never selected unless config asks for it."""

    name = "synthetic"

    def __init__(self, seed: int = 7, daily_vol: float = 0.02):
        self.seed = seed
        self.daily_vol = daily_vol

    def sample(self, histories: Sequence[pd.DataFrame], future_dates: Sequence[Sequence],
               n_paths: int) -> list[np.ndarray]:
        rng = np.random.default_rng(self.seed)
        out = []
        for h, dates in zip(histories, future_dates):
            horizon = len(dates)
            last = float(h["close"].iloc[-1])
            volume = float(h["volume"].iloc[-20:].mean())
            steps = rng.normal(0, self.daily_vol, size=(n_paths, horizon))
            close = last * np.exp(np.cumsum(steps, axis=1))
            open_ = np.concatenate([np.full((n_paths, 1), last), close[:, :-1]], axis=1)
            high = np.maximum(open_, close) * 1.005
            low = np.minimum(open_, close) * 0.995
            vol = np.full((n_paths, horizon), volume)
            out.append(np.stack([open_, high, low, close, vol], axis=-1))
        return out


# ------------------------------------------------------------------- checks
def candle_rule_breaks(paths: np.ndarray) -> dict[str, np.ndarray]:
    """Per bar rule, a (n_paths, horizon) bool array of the candles that break it."""
    n_paths, horizon, _ = paths.shape
    flat = pd.DataFrame(paths.reshape(-1, len(CANDLE)), columns=CANDLE)
    rules = {"non-finite value": ~np.isfinite(flat.to_numpy()).all(axis=1)}
    rules.update({rule: mask.to_numpy() for rule, mask in ohlcv_problem_masks(flat).items()})
    return {rule: mask.reshape(n_paths, horizon) for rule, mask in rules.items()}


def valid_candles(paths: np.ndarray) -> np.ndarray:
    """(n_paths, horizon) bool: True where the generated candle is a possible bar."""
    return ~np.logical_or.reduce(list(candle_rule_breaks(paths).values()))


def summarize(paths: np.ndarray, *, last_close: float, interval: tuple[float, float],
              max_invalid_pct: float, context: str) -> dict:
    """Close range per step from valid candles only, plus the invalid-candle count."""
    breaks = candle_rule_breaks(paths)
    valid = ~np.logical_or.reduce(list(breaks.values()))
    n_paths, horizon = valid.shape
    invalid = int((~valid).sum())
    invalid_pct = 100 * invalid / valid.size
    steps = []
    for s in range(horizon):
        closes = paths[valid[:, s], s, CLOSE]
        if len(closes) < MIN_VALID_PER_STEP:
            raise ProviderError(f"{context}: step {s + 1} has {len(closes)} valid candles of "
                                f"{n_paths} (need {MIN_VALID_PER_STEP}) — no range can be drawn")
        lo, mid, hi = np.quantile(closes, [interval[0], 0.5, interval[1]])
        steps.append({"close_low": float(lo), "close_median": float(mid),
                      "close_high": float(hi), "valid_paths": int(len(closes))})
    final = paths[valid[:, -1], -1, CLOSE]
    change = 100 * (final / last_close - 1)
    return {
        "steps": steps,
        "paths": n_paths,
        "candles": int(valid.size),
        "invalid_candles": invalid,
        "invalid_pct": round(invalid_pct, 1),
        # a candle can break several rules, so these may add up to more than invalid_candles
        "invalid_by_rule": {rule: int(mask.sum()) for rule, mask in breaks.items() if mask.any()},
        "max_invalid_pct": max_invalid_pct,
        "degraded": invalid_pct > max_invalid_pct,
        "final_close_change_pct": {
            "low": round(float(np.quantile(change, interval[0])), 2),
            "median": round(float(np.median(change)), 2),
            "high": round(float(np.quantile(change, interval[1])), 2),
        },
        "share_of_paths_up_pct": round(100 * float(np.mean(final > last_close)), 1),
    }


def backtest(ohlcv: pd.DataFrame, sampler: PathSampler, *, windows: int, horizon: int,
             n_paths: int, context_length: int, interval: tuple[float, float],
             max_invalid_pct: float, sessions_after) -> dict:
    """Rolling origins over the last `windows` sessions, in one sampling call.

    At origin o the model sees bars[:o] (at most `context_length`) and its range
    for the next `horizon` closes is compared with what happened. The naive error
    is "the close stays where it was" on the same points; for prices that is a
    hard baseline to beat, and the report says whether the median did.
    `sessions_after(last_date, n)` gives the dates the model is told about.
    """
    bars = ohlcv.reset_index(drop=True)
    n = len(bars)
    origins = [o for o in range(n - horizon - windows + 1, n - horizon + 1) if o >= 64]
    if not origins:
        raise ProviderError(f"Kronos backtest: {n} sessions is too short (need {64 + horizon})")
    histories = [bars.iloc[max(0, o - context_length):o] for o in origins]
    dates = [sessions_after(pd.Timestamp(h["timestamp"].iloc[-1]).date(), horizon) for h in histories]
    all_paths = sampler.sample(histories, dates, n_paths)

    errors, signed, naive, covered, invalid, candles = [], [], [], [], 0, 0
    for o, paths in zip(origins, all_paths):
        actual = bars["close"].to_numpy(dtype=float)[o:o + horizon]
        last = float(bars["close"].iloc[o - 1])
        s = summarize(paths, last_close=last, interval=interval, max_invalid_pct=max_invalid_pct,
                      context="Kronos backtest")
        invalid += s["invalid_candles"]
        candles += s["candles"]
        for step, real in zip(s["steps"], actual):
            errors.append(abs(step["close_median"] - real) / real)
            signed.append((step["close_median"] - real) / real)
            naive.append(abs(last - real) / real)
            covered.append(step["close_low"] <= real <= step["close_high"])
    mape, naive_mape = 100 * float(np.mean(errors)), 100 * float(np.mean(naive))
    return {
        "windows": len(origins),
        "horizon": horizon,
        "paths_per_window": n_paths,
        "points": len(errors),
        "median_error_pct": round(mape, 2),
        "naive_error_pct": round(naive_mape, 2),
        "skill_vs_naive_pct": round(100 * (1 - mape / naive_mape), 1) if naive_mape > 0 else None,
        # mean signed error of the median: below 0 = the model's middle ran low
        "median_bias_pct": round(100 * float(np.mean(signed)), 2),
        "coverage_pct": round(100 * float(np.mean(covered)), 1),
        "coverage_target_pct": round(100 * (interval[1] - interval[0]), 1),
        "invalid_pct": round(100 * invalid / candles, 1) if candles else 0.0,
    }


def to_frame(summary: dict, dates: Sequence, *, model: str) -> pd.DataFrame:
    steps = summary["steps"]
    return pd.DataFrame({
        "step": np.arange(1, len(steps) + 1),
        "date": pd.to_datetime([pd.Timestamp(d) for d in dates]).tz_localize("UTC"),
        "close_low": [s["close_low"] for s in steps],
        "close_median": [s["close_median"] for s in steps],
        "close_high": [s["close_high"] for s in steps],
        "valid_paths": [s["valid_paths"] for s in steps],
        "model": model,
    })
