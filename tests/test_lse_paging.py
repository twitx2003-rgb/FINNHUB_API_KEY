"""Option-chain paging and series integrity against simulated server behaviours.

Grounded in the first live LSE pull: the NVDA chain came back at exactly 5000
rows, which is the server's per-response cap, i.e. a truncated chain.
"""
import pandas as pd
import pytest

from pipeline.errors import ProviderError
from pipeline.providers.lse import LSEProvider, _MAX_ROWS
from pipeline.stages.data import atm_iv


def _contract(i, dte):
    return {
        "ticker": f"NVDA-{i:06d}", "contract_type": "call" if i % 2 else "put",
        "expiry": f"D+{dte}", "dte": dte, "strike": 100.0 + i % 300, "iv": 0.45,
        "delta": 0.5, "gamma": 0.01, "theta": -0.1, "vega": 0.2, "rho": 0.05,
        "last_price": 5.0, "underlying_price": 150.0, "volume_today": 10,
        "premium_today": 5000.0, "updated_at": "2026-09-22T18:00:00Z",
    }


class _Err(Exception):
    pass


class FakeChainServer:
    """Filters by DTE and clamps to the cap, like the vault. Knobs model the
    ways a real server could misbehave."""

    def __init__(self, contracts, inclusive=True, honour_dte=True):
        self.contracts, self.inclusive, self.honour_dte = contracts, inclusive, honour_dte
        self.calls = []

    def options(self, underlying, min_dte=None, max_dte=None, limit=5000, **_):
        self.calls.append((min_dte, max_dte))
        rows = self.contracts
        if self.honour_dte:
            if self.inclusive:
                rows = [r for r in rows if min_dte <= r["dte"] <= max_dte]
            else:
                rows = [r for r in rows if min_dte < r["dte"] < max_dte]
        return rows[:limit]


def _provider(server):
    p = LSEProvider.__new__(LSEProvider)
    p._client, p._LSEError = server, _Err
    return p


def test_small_chain_is_one_call():
    server = FakeChainServer([_contract(i, i % 90) for i in range(800)])
    chain = _provider(server).options_chain("NVDA", 90)
    assert len(chain) == 800 and len(server.calls) == 1


def test_capped_chain_is_paged_until_complete():
    contracts = [_contract(i, i % 91) for i in range(12_000)]
    server = FakeChainServer(contracts)
    chain = _provider(server).options_chain("NVDA", 90)
    assert len(chain) == 12_000                       # nothing lost to the cap
    assert chain["contract"].is_unique
    assert len(server.calls) > 2


def test_non_inclusive_bounds_are_detected_not_silently_lossy():
    server = FakeChainServer([_contract(i, i % 91) for i in range(12_000)], inclusive=False)
    with pytest.raises(ProviderError, match="not inclusive"):
        _provider(server).options_chain("NVDA", 90)


def test_server_ignoring_dte_filter_is_detected():
    server = FakeChainServer([_contract(i, i % 91) for i in range(12_000)], honour_dte=False)
    with pytest.raises(ProviderError):
        _provider(server).options_chain("NVDA", 90)


def test_single_expiry_over_the_cap_fails_loudly():
    server = FakeChainServer([_contract(i, 3) for i in range(_MAX_ROWS + 1)])
    with pytest.raises(ProviderError, match="alone reach"):
        _provider(server).options_chain("NVDA", 90)


# ------------------------------------------------------------ macro series
class FakeSeriesServer:
    def __init__(self, rows):
        self.rows = rows

    def bond_yields(self, symbol=None, order="asc", limit=5000, **_):
        return self.rows


def test_series_with_duplicate_dates_is_rejected():
    """Two instruments under one code would silently interleave into one line."""
    rows = [{"date": "2026-09-21", "close": 4.12}, {"date": "2026-09-21", "close": 3.01},
            {"date": "2026-09-18", "close": 4.08}]
    p = LSEProvider.__new__(LSEProvider)
    p._client, p._LSEError = FakeSeriesServer(rows), _Err
    with pytest.raises(ProviderError, match="more than one series"):
        p._series_frame("bond_yields", "US10Y", "YIELD_10Y")


def test_bond_yield_close_is_used_as_the_value():
    rows = [{"date": "2026-09-21", "open": 4.10, "high": 4.15, "low": 4.05, "close": 4.12},
            {"date": "2026-09-18", "open": 4.02, "high": 4.09, "low": 4.00, "close": 4.08}]
    p = LSEProvider.__new__(LSEProvider)
    p._client, p._LSEError = FakeSeriesServer(rows), _Err
    frame = p._series_frame("bond_yields", "US10Y", "YIELD_10Y")
    assert list(frame["value"]) == [4.08, 4.12]


# ----------------------------------------------------------------- ATM IV
def test_atm_iv_picks_nearest_30_day_expiry_and_strike():
    chain = pd.DataFrame({
        "dte": [7, 7, 28, 28, 28, 45], "strike": [230, 230, 220, 230, 240, 230],
        "implied_volatility": [1.90, 1.80, 0.52, 0.46, 0.49, 0.44],
    })
    assert atm_iv(chain, spot=229.0) == 0.46


def test_atm_iv_without_spot_is_none():
    chain = pd.DataFrame({"dte": [30], "strike": [230], "implied_volatility": [0.46]})
    assert atm_iv(chain, spot=None) is None
