"""Stage 3 — extract: insider transactions from SEC EDGAR (Form 4).

User decision (phase 4): the extract stage collects what company insiders
bought and sold — from SEC's official API, not by scraping. Any filing that
cannot be read exactly fails the stage: a silently skipped Form 4 would bias
the buy/sell picture the report shows.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

import pandas as pd

from .. import contracts
from ..cache import RunContext
from ..providers.sec_edgar import (
    ARCHIVE_URL,
    HOLDER_FORMS,
    SUBMISSIONS_URL,
    TICKERS_URL,
    SecClient,
    archive_url,
    cik_for,
    latest_positions,
    parse_form4,
    parse_schedule13,
    recent_filings,
    summarize,
)
from .base import Stage, StageResult

log = logging.getLogger(__name__)

ARTIFACT = "extract_insider"
HOLDERS_ARTIFACT = "extract_holders"
SEC_QUESTION = ("SEC asks everyone using its data for a name and e-mail (sent only to sec.gov).\nType yours, for example:  Jane Doe jane@example.com")
COLUMNS = ["accession", "insider", "role", "date", "code", "meaning", "direction", "shares",
           "price", "shares_after", "ownership", "plan_10b5_1"]


def default_client(ctx: RunContext) -> SecClient:
    agent = ctx.settings.env_or_ask("SEC_USER_AGENT", SEC_QUESTION, must_contain="@")
    return SecClient(agent, ctx.settings.cache_dir / "sec")


class ExtractStage(Stage):
    name = "extract"

    def __init__(self, client_factory: Callable[[RunContext], SecClient] = default_client):
        self.client_factory = client_factory

    def run(self, ctx: RunContext) -> StageResult:
        cfg = ctx.settings.extract
        if not cfg.sec_enabled:
            return StageResult(stage=self.name, status="skipped", summary="SEC extract disabled")

        client = self.client_factory(ctx)
        cik = cik_for(client.json(TICKERS_URL), ctx.ticker)
        submissions = client.json(SUBMISSIONS_URL.format(cik=cik))
        since = (datetime.now(timezone.utc) - timedelta(days=cfg.sec_lookback_days)).date()
        filings = recent_filings(submissions, {"4"}, since)
        amendments = len(recent_filings(submissions, {"4/A"}, since))
        truncated = max(0, len(filings) - cfg.sec_max_filings)
        filings = filings[:cfg.sec_max_filings]
        log.info("SEC: %s is CIK %d; %d Form 4 filing(s) since %s%s", ctx.ticker, cik, len(filings),
                 since, f" (oldest {truncated} beyond the cap not read)" if truncated else "")

        rows = []
        for filing in filings:
            xml = client.fetch(archive_url(cik, filing), cache=True)
            rows.extend(parse_form4(xml, filing.accession, expected_cik=cik))

        frame = pd.DataFrame(rows, columns=COLUMNS)
        for column in ("shares", "price", "shares_after"):
            frame[column] = pd.to_numeric(frame[column])
        ctx.write_parquet(ARTIFACT, frame, contracts.INSIDER)
        summary = summarize(rows)
        ctx.write_json(ARTIFACT, {
            "source": "SEC EDGAR Form 4", "cik": cik, "since": since.isoformat(),
            "filings_read": len(filings), "filings_beyond_cap": truncated,
            "amendments_not_read": amendments, "summary": summary,
        })

        holders = self._holders(ctx, client, cik, submissions)
        ctx.write_json(HOLDERS_ARTIFACT, holders)

        buys, sales = summary["open_market_buys"], summary["open_market_sales"]
        log.info("insiders: %d open-market buys (%.0f shares), %d sales (%.0f shares, $%.0f), "
                 "%s%% of sold shares under 10b5-1 plans", buys["count"], buys["shares"],
                 sales["count"], sales["shares"], sales["value_usd"],
                 summary["sale_shares_under_10b5_1_pct"])
        top = ", ".join(f"{h['holder']} {h['percent']}%" for h in holders["holders"][:3]) or "none"
        return StageResult(
            stage=self.name, status="ok",
            summary=(f"{len(rows)} insider transaction(s) from {len(filings)} Form 4 filing(s) "
                     f"since {since}: {buys['count']} open-market buys, {sales['count']} sales; "
                     f">5% holders: {top}"),
            artifacts=[ARTIFACT, HOLDERS_ARTIFACT], details={"insider_summary": summary})

    def _holders(self, ctx: RunContext, client: SecClient, cik: int, submissions) -> dict:
        cfg = ctx.settings.extract
        since = (datetime.now(timezone.utc) - timedelta(days=cfg.sec_holders_lookback_days)).date()
        filings = recent_filings(submissions, HOLDER_FORMS, since)
        rows, filed, unreadable = [], {}, []
        for f in filings:
            name = f.primary_document.rsplit("/", 1)[-1]
            if not name.lower().endswith(".xml"):
                # Pre-2025 filings are free text; reading them would mean guessing.
                unreadable.append({"accession": f.accession, "form": f.form, "filed": f.filed.isoformat()})
                continue
            url = ARCHIVE_URL.format(cik=cik, folder=f.accession.replace("-", ""), name=name)
            rows.extend(parse_schedule13(client.fetch(url, cache=True), f.accession))
            filed[f.accession] = f.filed.isoformat()
        positions = latest_positions(rows, filed, cik)
        for h in positions["holders"]:
            log.info("holder %s: %.2f%% (%s shares, filed %s)", h["holder"], h["percent"],
                     f"{h['shares']:,.0f}", h["filed"])
        if unreadable:
            log.warning("%d older Schedule 13 filing(s) are free text and were not read", len(unreadable))
        return {"source": "SEC EDGAR Schedule 13G/13D", "since": since.isoformat(), **positions,
                "not_machine_readable": unreadable,
                "note": "Only filings listed in the company's own EDGAR submissions; "
                        "holders below 5% file no Schedule 13."}
