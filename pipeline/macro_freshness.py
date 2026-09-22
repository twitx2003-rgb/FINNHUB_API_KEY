"""Macro series freshness, with a per-series fallback.

The price-bar freshness check never covered macro series, and the first full
LSE run showed why it has to: the daily US 10-year yield ended 12 days before
the run date while the rest of the pull was current. A daily series that far
behind would quietly feed a stale rate into forecasts and the report.

Each series is judged against a limit for its own frequency. Monthly limits
are generous on purpose: CPI for a month is published about two weeks after
the month ends, so just before a release the newest observation is already
~70 days old and that is normal.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import pandas as pd

log = logging.getLogger(__name__)

DAILY = "daily"
MONTHLY = "monthly"


@dataclass(frozen=True)
class SeriesCheck:
    series: str
    frequency: str
    latest: str
    age_days: int
    max_age_days: int
    source: str            # where the kept rows came from
    action: str            # ok | replaced | stale_kept

    @property
    def stale(self) -> bool:
        return self.age_days > self.max_age_days

    def as_dict(self) -> dict:
        return {"frequency": self.frequency, "latest": self.latest, "age_days": self.age_days,
                "max_age_days": self.max_age_days, "source": self.source, "action": self.action}


def frequency(timestamps: pd.Series) -> str:
    """Daily if observations are typically a few days apart, else monthly."""
    ts = pd.to_datetime(timestamps, utc=True).sort_values()
    if len(ts) < 3:
        return MONTHLY
    return DAILY if ts.diff().dropna().dt.days.median() <= 7 else MONTHLY


def _age_days(part: pd.DataFrame, now: datetime) -> tuple[str, int]:
    newest = pd.to_datetime(part["timestamp"], utc=True).max()
    return str(newest.date()), (now - newest.to_pydatetime()).days


def refresh_stale_series(
    macro: pd.DataFrame,
    *,
    max_age_daily: int,
    max_age_monthly: int,
    primary: str,
    fetch_fallback: Callable[[str], pd.DataFrame] | None,
    fallback_name: str = "fred",
    now: datetime | None = None,
) -> tuple[pd.DataFrame, list[SeriesCheck]]:
    """Replace each stale series with the fallback's copy when that copy is fresher.

    Only the stale series is replaced; the rest keep coming from the primary
    provider. If the fallback fails, or is no fresher, the original rows are
    kept and the check is reported as `stale_kept` — never silently passed.
    """
    now = now or datetime.now(timezone.utc)
    if macro.empty:
        return macro, []

    kept: list[pd.DataFrame] = []
    checks: list[SeriesCheck] = []

    for label, part in macro.groupby("series", sort=True):
        freq = frequency(part["timestamp"])
        limit = max_age_daily if freq == DAILY else max_age_monthly
        latest, age = _age_days(part, now)

        if age <= limit:
            kept.append(part)
            checks.append(SeriesCheck(label, freq, latest, age, limit, primary, "ok"))
            continue

        log.warning("macro %s from %s is stale: latest %s is %d days old (limit %d for %s data)",
                    label, primary, latest, age, limit, freq)

        replacement = None
        if fetch_fallback is not None:
            try:
                candidate = fetch_fallback(label)
            except Exception as exc:  # noqa: BLE001 — any fallback failure keeps the original
                log.warning("macro %s: %s fallback failed: %s", label, fallback_name, exc)
                candidate = None
            if candidate is not None and not candidate.empty:
                new_latest, new_age = _age_days(candidate, now)
                if new_age < age:
                    replacement = candidate
                    log.warning("macro %s: using %s instead (latest %s, %d days old)",
                                label, fallback_name, new_latest, new_age)
                    checks.append(SeriesCheck(label, freq, new_latest, new_age, limit,
                                              fallback_name, "replaced"))
                else:
                    log.warning("macro %s: %s is no fresher (latest %s) — keeping %s",
                                label, fallback_name, new_latest, primary)

        if replacement is None:
            kept.append(part)
            checks.append(SeriesCheck(label, freq, latest, age, limit, primary, "stale_kept"))
        else:
            kept.append(replacement)

    out = pd.concat(kept, ignore_index=True).sort_values(["series", "timestamp"])
    return out.reset_index(drop=True), checks
