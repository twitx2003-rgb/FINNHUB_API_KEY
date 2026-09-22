"""lse-data response mapping, built from the key set of a live 0.14.0 response."""
import pandas as pd
import pytest

from pipeline.contracts import OPTIONS_CHAIN, OPTIONS_COLUMNS
from pipeline.errors import ProviderError
from pipeline.providers.base import pick
from pipeline.providers.lse import LSEProvider


# Exact key set observed on the user's machine, 2026-09-22.
LIVE_OPTION_KEYS = [
    "contract_type", "delta", "dte", "expiry", "gamma", "iv", "last_price",
    "last_trade_at", "premium_today", "rho", "strike", "theta", "ticker",
    "underlying", "underlying_price", "updated_at", "vega", "volume_today",
]


def _row(**overrides):
    row = {
        "contract_type": "call", "delta": 0.52, "dte": 25, "expiry": "2026-10-16",
        "gamma": 0.011, "iv": 0.44, "last_price": 9.85, "last_trade_at": "2026-09-22T18:40:00Z",
        "premium_today": 1_250_000.0, "rho": 0.07, "strike": 230.0, "theta": -0.19,
        "ticker": "NVDA261016C00230000", "underlying": "NVDA", "underlying_price": 150.0,
        "updated_at": "2026-09-22T18:49:00Z", "vega": 0.26, "volume_today": 12840,
    }
    assert sorted(row) == sorted(LIVE_OPTION_KEYS)
    row.update(overrides)
    return row


class _FakeLSEError(Exception):
    pass


class _FakeClient:
    def __init__(self, option_rows):
        self.option_rows = option_rows

    def options(self, **kwargs):
        return self.option_rows


def _provider(option_rows):
    provider = LSEProvider.__new__(LSEProvider)   # skip the real client/API key
    provider._client = _FakeClient(option_rows)
    provider._LSEError = _FakeLSEError
    return provider


# ---------------------------------------------------------------- pick()
def test_pick_distinguishes_null_from_missing():
    with pytest.raises(ProviderError, match="present but null"):
        pick({"close": None}, ("close",), context="t")
    with pytest.raises(ProviderError, match="none of"):
        pick({"other": 1}, ("close",), context="t")


def test_pick_allows_null_only_when_asked():
    assert pick({"iv": None}, ("iv",), context="t", allow_null=True) is None


# ---------------------------------------------------------- live mapping
def test_live_chain_with_an_untraded_contract_maps_cleanly():
    """The failure from the first LSE run: one contract had iv=None."""
    rows = [_row(), _row(ticker="NVDA261016C00400000", strike=400.0, iv=None,
                         delta=None, gamma=None, theta=None, vega=None, rho=None,
                         last_price=None, volume_today=0)]
    chain = _provider(rows).options_chain("NVDA", 90)

    OPTIONS_CHAIN.validate(chain)
    assert list(chain.columns) == list(OPTIONS_COLUMNS)
    assert chain.loc[0, "implied_volatility"] == 0.44
    assert pd.isna(chain.loc[1, "implied_volatility"])      # null kept as NaN, not guessed


def test_volume_comes_from_volume_today_not_a_default_zero():
    chain = _provider([_row(volume_today=12840)]).options_chain("NVDA", 90)
    assert chain.loc[0, "volume"] == 12840


def test_type_and_contract_come_from_live_field_names():
    chain = _provider([_row()]).options_chain("NVDA", 90)
    assert chain.loc[0, "type"] == "call"
    assert chain.loc[0, "contract"] == "NVDA261016C00230000"
    assert chain.loc[0, "rho"] == 0.07 and chain.loc[0, "underlying_price"] == 150.0


def test_missing_strike_still_fails_loudly():
    row = _row(); del row["strike"]
    with pytest.raises(ProviderError, match="Actual keys"):
        _provider([row]).options_chain("NVDA", 90)


def test_null_strike_is_rejected():
    with pytest.raises(ProviderError, match="present but null"):
        _provider([_row(strike=None)]).options_chain("NVDA", 90)


def test_summary_exposes_iv_scale_and_null_share():
    from pipeline.stages.data import summarize_options
    rows = [_row(), _row(ticker="X", iv=None, delta=None, volume_today=0)]
    summary = summarize_options(_provider(rows).options_chain("NVDA", 90))
    assert summary["iv_median"] == 0.44
    assert summary["iv_null_pct"] == 50
    assert summary["traded_today"] == 1
