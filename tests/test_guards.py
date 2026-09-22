"""The ordering / freshness / sanity guards — the defences against bad data."""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from pipeline.contracts import OHLCV, ContractError, assert_ohlcv_sane
from pipeline.errors import OrderingError, ProviderError, StaleDataError
from pipeline.providers.base import assert_descending, assert_fresh, pick, pick_optional


def _rows(dates):
    return [{"timestamp": d, "close": 1.0} for d in dates]


def test_assert_descending_accepts_newest_first():
    assert_descending(_rows(["2026-09-20", "2026-09-19", "2026-09-18"]),
                      "timestamp", context="t")


def test_assert_descending_rejects_oldest_first():
    """The lse-data trap: order='asc' would hand back 2003 first."""
    with pytest.raises(OrderingError, match="ignored the ordering"):
        assert_descending(_rows(["2003-01-02", "2003-01-03", "2003-01-06"]),
                          "timestamp", context="lse candles(NVDA)")


def test_assert_descending_ignores_single_row():
    assert_descending(_rows(["2026-09-20"]), "timestamp", context="t")


def test_assert_fresh_passes_for_recent_data():
    now = datetime.now(timezone.utc)
    df = pd.DataFrame({"timestamp": pd.to_datetime([now - timedelta(days=1)], utc=True)})
    assert_fresh(df, "timestamp", 5, context="t")


def test_assert_fresh_rejects_start_of_history():
    df = pd.DataFrame({"timestamp": pd.to_datetime(["2003-01-02"], utc=True)})
    with pytest.raises(StaleDataError, match="Refusing to build on stale data"):
        assert_fresh(df, "timestamp", 5, context="lse candles(NVDA)")


def test_assert_fresh_rejects_empty():
    with pytest.raises(ProviderError):
        assert_fresh(pd.DataFrame({"timestamp": []}), "timestamp", 5, context="t")


def test_pick_returns_first_present():
    assert pick({"ts": 5}, ("timestamp", "ts"), context="t") == 5


def test_pick_fails_loudly_listing_actual_keys():
    with pytest.raises(ProviderError, match="Actual keys"):
        pick({"weird": 1}, ("iv", "implied_volatility"), context="lse options")


def test_pick_optional_defaults_instead_of_raising():
    assert pick_optional({}, ("volume",), 0.0) == 0.0


def _good_bars():
    return pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-09-18", "2026-09-19"], utc=True),
        "symbol": ["NVDA"] * 2,
        "open": [100.0, 102.0], "high": [105.0, 103.0],
        "low": [99.0, 101.0], "close": [104.0, 101.5],
        "volume": [1e6, 2e6],
    })


def test_ohlcv_contract_accepts_good_frame():
    OHLCV.validate(_good_bars())
    assert_ohlcv_sane(_good_bars())


def test_ohlcv_contract_rejects_missing_column():
    with pytest.raises(ContractError, match="missing column"):
        OHLCV.validate(_good_bars().drop(columns=["volume"]))


def test_ohlcv_contract_rejects_empty():
    with pytest.raises(ContractError, match="no rows"):
        OHLCV.validate(_good_bars().iloc[0:0])


def test_sanity_rejects_high_below_body():
    bad = _good_bars()
    bad.loc[0, "high"] = 50.0          # below open/close
    with pytest.raises(ContractError, match=r"high < max"):
        assert_ohlcv_sane(bad)


def test_sanity_rejects_low_above_body():
    bad = _good_bars()
    bad.loc[0, "low"] = 200.0
    with pytest.raises(ContractError, match=r"low > min"):
        assert_ohlcv_sane(bad)


def test_sanity_rejects_negative_volume():
    bad = _good_bars()
    bad.loc[1, "volume"] = -1.0
    with pytest.raises(ContractError, match="negative volume"):
        assert_ohlcv_sane(bad)
