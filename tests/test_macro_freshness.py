"""Per-series macro freshness and FRED fallback.

Grounded in the first full LSE run: the daily US 10-year yield ended 12 days
before the run date while every other series was current.
"""
from datetime import datetime, timezone

import pandas as pd

from pipeline.macro_freshness import DAILY, MONTHLY, frequency, refresh_stale_series

NOW = datetime(2026, 9, 22, 20, 0, tzinfo=timezone.utc)


def _daily(label, end, n=30, value=4.0, code="US10Y"):
    days = pd.bdate_range(end=end, periods=n, tz="UTC")
    return pd.DataFrame({"timestamp": days, "series": label, "source_symbol": code,
                         "value": [value + i * 0.01 for i in range(n)]})


def _monthly(label, end, n=24, code="usacsa"):
    months = pd.date_range(end=end, periods=n, freq="MS", tz="UTC")
    return pd.DataFrame({"timestamp": months, "series": label, "source_symbol": code,
                         "value": [300.0 + i for i in range(n)]})


def _refresh(macro, fallback):
    return refresh_stale_series(macro, max_age_daily=7, max_age_monthly=75,
                                primary="lse", fetch_fallback=fallback, now=NOW)


def test_frequency_detection():
    assert frequency(_daily("Y", "2026-09-21")["timestamp"]) == DAILY
    assert frequency(_monthly("C", "2026-08-01")["timestamp"]) == MONTHLY


def test_fresh_series_never_call_the_fallback():
    calls = []
    macro = pd.concat([_daily("YIELD_10Y", "2026-09-21"), _monthly("CPI", "2026-08-01")])
    out, checks = _refresh(macro, lambda label: calls.append(label))
    assert calls == []
    assert {c.action for c in checks} == {"ok"}
    assert len(out) == len(macro)


def test_stale_daily_series_is_replaced_by_a_fresher_fallback():
    """The live case: yield stuck 12 days back, CPI fine."""
    macro = pd.concat([_daily("YIELD_10Y", "2026-09-10"), _monthly("CPI", "2026-08-01")])
    fred = _daily("YIELD_10Y", "2026-09-21", code="DGS10", value=4.5)
    out, checks = _refresh(macro, lambda label: fred if label == "YIELD_10Y" else None)

    by_series = {c.series: c for c in checks}
    assert by_series["YIELD_10Y"].action == "replaced"
    assert by_series["YIELD_10Y"].source == "fred"
    assert by_series["CPI"].action == "ok" and by_series["CPI"].source == "lse"

    yields = out[out["series"] == "YIELD_10Y"]
    assert set(yields["source_symbol"]) == {"DGS10"}           # no LSE rows mixed in
    assert yields["timestamp"].max().date().isoformat() == "2026-09-21"
    assert set(out[out["series"] == "CPI"]["source_symbol"]) == {"usacsa"}


def test_monthly_series_just_before_a_release_is_not_stale():
    """On 10 Sep the newest CPI is July's (label 07-01, ~71 days old) — normal."""
    early = datetime(2026, 9, 10, tzinfo=timezone.utc)
    _, checks = refresh_stale_series(_monthly("CPI", "2026-07-01"), max_age_daily=7,
                                     max_age_monthly=75, primary="lse",
                                     fetch_fallback=None, now=early)
    assert checks[0].action == "ok"


def test_monthly_series_missing_a_published_month_is_stale():
    """On 22 Sep, August CPI is out; a copy ending at July (83 days) is behind."""
    _, checks = _refresh(_monthly("CPI", "2026-07-01"), fallback=None)
    assert checks[0].action == "stale_kept"


def test_failing_fallback_keeps_original_and_reports_it():
    macro = _daily("YIELD_10Y", "2026-09-10")

    def boom(label):
        raise ConnectionError("FRED unreachable")

    out, checks = _refresh(macro, boom)
    assert checks[0].action == "stale_kept"
    assert checks[0].stale
    assert len(out) == len(macro)


def test_fallback_that_is_not_fresher_is_not_used():
    macro = _daily("YIELD_10Y", "2026-09-10")
    older = _daily("YIELD_10Y", "2026-09-01", code="DGS10")
    out, checks = _refresh(macro, lambda label: older)
    assert checks[0].action == "stale_kept"
    assert set(out["source_symbol"]) == {"US10Y"}
