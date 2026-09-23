"""Artifact schemas.

Every Parquet file the pipeline writes is checked against a contract first, so a
provider change surfaces here as a named error rather than three stages later as
a confusing KeyError.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .errors import ContractError

NUMERIC = "numeric"
DATETIME = "datetime"
STRING = "string"
ANY = "any"


@dataclass(frozen=True)
class Contract:
    name: str
    columns: dict[str, str]           # column -> NUMERIC | DATETIME | STRING | ANY
    required_non_null: tuple[str, ...] = ()
    allow_empty: bool = False

    def validate(self, df: pd.DataFrame) -> pd.DataFrame:
        if not isinstance(df, pd.DataFrame):
            raise ContractError(f"{self.name}: expected a DataFrame, got {type(df).__name__}")

        missing = [c for c in self.columns if c not in df.columns]
        if missing:
            raise ContractError(
                f"{self.name}: missing column(s) {missing}. Present: {sorted(df.columns)}"
            )

        if df.empty and not self.allow_empty:
            raise ContractError(f"{self.name}: no rows returned")

        for column, kind in self.columns.items():
            series = df[column]
            if kind == NUMERIC and not pd.api.types.is_numeric_dtype(series):
                raise ContractError(
                    f"{self.name}.{column}: expected numeric, got dtype {series.dtype}"
                )
            if kind == DATETIME and not pd.api.types.is_datetime64_any_dtype(series):
                raise ContractError(
                    f"{self.name}.{column}: expected datetime, got dtype {series.dtype}"
                )

        for column in self.required_non_null:
            if df[column].isna().any():
                bad = int(df[column].isna().sum())
                raise ContractError(f"{self.name}.{column}: {bad} null value(s), none allowed")

        return df


OHLCV = Contract(
    name="data_ohlcv",
    columns={
        "timestamp": DATETIME,
        "symbol": STRING,
        "open": NUMERIC,
        "high": NUMERIC,
        "low": NUMERIC,
        "close": NUMERIC,
        "volume": NUMERIC,
    },
    required_non_null=("timestamp", "close"),
)

# Every provider's option chain is reindexed to exactly these columns, so the
# artifact has one schema whichever vendor produced it. A vendor that lacks a
# field (yfinance has no greeks, lse-data has no open interest) leaves it NaN.
OPTIONS_COLUMNS: tuple[str, ...] = (
    "underlying", "contract", "expiry", "dte", "strike", "type",
    "last_price", "underlying_price", "implied_volatility",
    "delta", "gamma", "theta", "vega", "rho",
    "volume", "premium", "open_interest", "updated_at",
)
OPTIONS_NUMERIC: tuple[str, ...] = (
    "dte", "strike", "last_price", "underlying_price", "implied_volatility",
    "delta", "gamma", "theta", "vega", "rho", "volume", "premium", "open_interest",
)

def normalize_options(frame: pd.DataFrame) -> pd.DataFrame:
    """Coerce a provider's chain to the canonical OPTIONS_COLUMNS schema."""
    frame = frame.reindex(columns=list(OPTIONS_COLUMNS))
    for column in OPTIONS_NUMERIC:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    for column in ("underlying", "contract", "expiry", "type", "updated_at"):
        frame[column] = frame[column].astype("string")
    frame["type"] = frame["type"].str.lower()
    return frame


OPTIONS_CHAIN = Contract(
    name="data_options",
    columns={
        "underlying": STRING,
        "expiry": ANY,
        "strike": NUMERIC,
        "type": STRING,
        "implied_volatility": NUMERIC,
        "delta": NUMERIC,
        "gamma": NUMERIC,
        "theta": NUMERIC,
        "vega": NUMERIC,
    },
    # Chains legitimately come back empty outside market hours / for odd tickers.
    allow_empty=True,
)

MACRO = Contract(
    name="data_macro",
    columns={"timestamp": DATETIME, "series": STRING, "value": NUMERIC},
    required_non_null=("timestamp", "series"),
    allow_empty=True,
)


FORECAST = Contract(
    name="forecast_timesfm",
    columns={"series": STRING, "step": NUMERIC, "date": DATETIME, "q_low": NUMERIC,
             "median": NUMERIC, "q_high": NUMERIC, "model": STRING},
    required_non_null=("series", "step", "date", "q_low", "median", "q_high"),
)


INSIDER = Contract(
    name="extract_insider",
    columns={"accession": STRING, "insider": STRING, "role": STRING, "date": STRING,
             "code": STRING, "direction": STRING, "shares": NUMERIC, "price": NUMERIC,
             "shares_after": NUMERIC, "plan_10b5_1": ANY},
    required_non_null=("accession", "insider", "date", "code", "direction", "shares"),
    allow_empty=True,                 # a quiet half-year has no Form 4s
)


SCENARIOS = Contract(
    name="forecast_kronos",
    columns={"step": NUMERIC, "date": DATETIME, "close_low": NUMERIC, "close_median": NUMERIC,
             "close_high": NUMERIC, "valid_paths": NUMERIC, "model": STRING},
    required_non_null=("step", "date", "close_low", "close_median", "close_high", "valid_paths"),
)


def ohlcv_problem_masks(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Bar rules as one boolean mask per rule (True = the bar breaks it).

    Shared by assert_ohlcv_sane (real bars: any break is an error) and the Kronos
    scenario check (generated candles: breaking bars are dropped and counted).
    """
    body_high = df[["open", "close"]].max(axis=1)
    body_low = df[["open", "close"]].min(axis=1)
    return {
        "high < max(open, close)": df["high"] < body_high - 1e-6,
        "low > min(open, close)": df["low"] > body_low + 1e-6,
        "negative volume": df["volume"] < 0,
        "non-positive price": (df[["open", "high", "low", "close"]] <= 0).any(axis=1),
    }


def assert_ohlcv_sane(df: pd.DataFrame) -> pd.DataFrame:
    """Bar-level invariants. Cheap here, and they catch provider bugs early."""
    if df.empty:
        return df
    problems = [rule for rule, mask in ohlcv_problem_masks(df).items() if mask.any()]
    if problems:
        raise ContractError(f"data_ohlcv: impossible bars -> {'; '.join(problems)}")
    return df
