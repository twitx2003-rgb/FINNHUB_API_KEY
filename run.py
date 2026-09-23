#!/usr/bin/env python
"""Orchestrator.

    python run.py --ticker NVDA --stages all
    python run.py --ticker NVDA --stages data,validate
    python run.py --discover-macro
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from pipeline.cache import make_run_context
from pipeline.config import load_settings
from pipeline.errors import PipelineError, PipelineHalt
from pipeline.logging_setup import setup_logging
from pipeline.stages import build_registry, resolve_stages

log = logging.getLogger("run")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run.py", description="Personal capital-market research pipeline"
    )
    parser.add_argument("--ticker", help="Symbol to analyse, e.g. NVDA")
    parser.add_argument("--stages", default="all",
                        help="'all' or a comma list: data,validate,extract,docs,forecast,debate,report")
    parser.add_argument("--run-date", help="Reuse a previous run directory (YYYY-MM-DD)")
    parser.add_argument("--config", help="Path to config.yaml")
    parser.add_argument("--discover-macro", nargs="?", const="", default=None, metavar="TERM",
                        help="Search the LSE macro catalogue (e.g. --discover-macro cpi), "
                             "then exit. Without TERM, lists the first entries.")
    parser.add_argument("--discover-fundamentals", metavar="SYMBOL",
                        help="Show the raw market-cap and earnings-date fields the providers "
                             "return for SYMBOL (phase 2 mapping), then exit")
    parser.add_argument("--auth-tradingview", action="store_true",
                        help="Sign in to TradingView's MCP server in the browser (once), then exit")
    parser.add_argument("--tradingview-diagnose", action="store_true",
                        help="Show which OAuth sign-in routes TradingView's server offers, then exit")
    parser.add_argument("--tradingview-probe", metavar="URL",
                        help="Follow a TradingView sign-in URL hop by hop and report where it "
                             "is blocked, then exit")
    parser.add_argument("--tradingview-tools", action="store_true",
                        help="List the tools TradingView's MCP server offers, then exit")
    parser.add_argument("--tradingview-call", nargs="+", metavar=("TOOL", "KEY=VALUE"),
                        help="Call one TradingView tool and print the raw result, e.g. "
                             "--tradingview-call mcp-tv-get-ohlcv symbol=NASDAQ:NVDA count=5")
    parser.add_argument("--discover-session", nargs=2, metavar=("SYMBOL", "DATE"),
                        help="Rebuild one day from LSE 5-minute candles and show whether the "
                             "daily bar is the regular session or includes extended hours")
    parser.add_argument("--tradingview-token-status", action="store_true",
                        help="Show whether a TradingView sign-in is stored, when it expires and "
                             "whether it can be renewed (prints no secrets)")
    parser.add_argument("--seed-saved-answers", metavar="TICKER",
                        help="One-off: save the TradingView answers already in "
                             "logs/tradingview_payloads/ as the ticker's saved answers, dated by "
                             "when each file was written")
    parser.add_argument("--docs-spike", action="store_true",
                        help="Phase 4 spike: measure whether page-screenshot retrieval finds the "
                             "right page in docs_input/*.pdf for the questions in "
                             "docs_input/questions.yaml (recall@5)")
    parser.add_argument("--docs-dtype", default="float32", choices=["float32", "bfloat16"],
                        help="Number format for the embedding model on CPU. float32 (default) "
                             "needs ~9 GB RAM and is fast on most CPUs; bfloat16 halves the "
                             "memory but is emulated (much slower) on CPUs without native bf16")
    parser.add_argument("--discover-sec", metavar="TICKER",
                        help="Show what SEC EDGAR returns for a ticker: CIK, recent form types, "
                             "and the first Form 4 parsed")
    parser.add_argument("--check-docs-model", action="store_true",
                        help="Load the page-embedding model (downloads on first use), embed two "
                             "generated pages and check a question finds the right one")
    parser.add_argument("--check-timesfm", action="store_true",
                        help="Load the TimesFM model (downloads the weights on first use) and "
                             "forecast a known test series, to prove the install works")
    parser.add_argument("--selftest", action="store_true",
                        help="Run the pipeline against synthetic data (no API key, no network) "
                             "to verify the installation, then exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


_DESCRIPTIVE_KEYS = ("description", "title", "name", "label", "long_name", "category",
                     "country", "region", "frequency", "unit", "units")
_SPAN_KEYS = ("last_value", "last_tick", "first_tick", "ticks", "years",
              "start", "end", "observations", "count")


def discover_macro(settings, term: str = "") -> int:
    """Search the macro catalogue. Matches TERM against every field of a row,
    because the catalogue's codes (e.g. 'usaeffr') are terse and the words that
    identify a series usually live in its description."""
    from pipeline.providers import get_provider

    provider = get_provider("lse", settings)
    rows = provider.list_economics()
    if rows:
        print(f"\ncatalogue fields: {sorted(rows[0])}")

    needle = (term or "").strip().lower()
    matches = [r for r in rows if not needle or any(needle in str(v).lower() for v in r.values())]
    shown = matches[:60]
    scope = f"matching '{term}'" if needle else "in total"
    print(f"{len(matches)} of {len(rows)} macro series {scope}. Showing {len(shown)}:\n")

    for row in shown:
        code = row.get("symbol") or row.get("code") or row.get("name") or "?"
        about = [str(row[k]) for k in _DESCRIPTIVE_KEYS if row.get(k) and row.get(k) != code]
        span = [f"{k}={row[k]}" for k in _SPAN_KEYS if row.get(k)]
        print(f"  {str(code):<22} {' | '.join(about)[:80]}")
        if span:
            print(f"  {'':<22} {'  '.join(span)[:80]}")

    if needle in ("", "yield", "bond", "10y"):
        print("\nBond-yield tenors are a separate table; the default US10Y is a valid code.")
    print("\nPut the codes you want in config.yaml -> data.cpi_series / data.yield_series")
    print("Try: --discover-macro cpi   --discover-macro inflation   --discover-macro usa\n")
    return 0


def discover_fundamentals(settings, symbol: str) -> int:
    """Print what LSE and Yahoo return for market cap / earnings dates.

    Stage 2 compares these against TradingView, but neither shape has been seen
    live yet — so look before mapping, as with the option chain.
    """
    from pipeline.providers import get_provider

    print(f"\n== LSE fundamentals({symbol}) ==")
    try:
        rows = get_provider("lse", settings).fundamentals_rows(symbol)
        print(f"{len(rows)} row(s)")
        for key, value in sorted((rows[0] if rows else {}).items()):
            print(f"  {key:<32} {str(value)[:70]}")
    except Exception as exc:  # noqa: BLE001 — discovery reports whatever happened
        print(f"  failed: {type(exc).__name__}: {exc}")

    print(f"\n== Yahoo calendar({symbol}) — LSE has no earnings dates ==")
    try:
        import yfinance as yf

        for key, value in (yf.Ticker(symbol).calendar or {}).items():
            print(f"  {key:<32} {value!r}"[:110])
    except Exception as exc:  # noqa: BLE001
        print(f"  failed: {type(exc).__name__}: {exc}")
    print()
    return 0


def discover_session(settings, symbol: str, day_text: str) -> int:
    """Print what LSE's daily bar for DAY is made of (regular vs extended hours)."""
    from datetime import date, timedelta

    from pipeline.providers import get_provider
    from pipeline.session_check import session_summary

    day = date.fromisoformat(day_text)
    lse = get_provider("lse", settings)
    # 20:00 New York is already the next day in UTC, so fetch two days and filter.
    intraday = lse.intraday(symbol, "5m", day.isoformat(), (day + timedelta(days=2)).isoformat())
    daily_frame = lse.intraday(symbol, "1d", day.isoformat(), (day + timedelta(days=1)).isoformat())
    daily = None
    if not daily_frame.empty:
        same_day = daily_frame[daily_frame["timestamp"].dt.date == day]
        if not same_day.empty:
            daily = {k: float(same_day.iloc[0][k]) for k in ("open", "high", "low", "close", "volume")}

    summary = session_summary(intraday, daily, day, settings.data.market_timezone)
    print(f"\n== {symbol} {day} — LSE 5-minute candles, New York time ==")
    if "error" in summary:
        print(f"  {summary['error']}\n")
        return 1
    for name in ("pre_market", "regular", "after_hours", "whole_day"):
        p = summary[name]
        if p is None:
            print(f"  {name:<12} (no bars)")
            continue
        print(f"  {name:<12} {p['first_bar']}-{p['last_bar']}  open {p['open']:.2f}  "
              f"close {p['close']:.2f}  high {p['high']:.2f}  low {p['low']:.2f}  "
              f"volume {p['volume']:,.0f}  ({p['bars']} bars)")
    if daily is None:
        print("  daily bar   (none for this day)\n")
        return 0
    print(f"  daily bar    open {daily['open']:.2f}  close {daily['close']:.2f}  "
          f"high {daily['high']:.2f}  low {daily['low']:.2f}  volume {daily['volume']:,.0f}")
    print(f"\n  daily close matches: {', '.join(summary['daily_close_matches'])}")
    print(f"  daily volume = {summary['daily_volume_vs_regular_pct']}% of regular-session volume, "
          f"{summary['daily_volume_vs_whole_day_pct']}% of the whole day\n")
    return 0


def make_tradingview(settings, interactive: bool = False):
    from pipeline.providers.tradingview_mcp import TradingViewMCP

    v = settings.validate
    return TradingViewMCP(
        url=v.tradingview_url,
        token_path=v.tradingview_token_path,
        callback_host=v.tradingview_callback_host,
        callback_port=v.tradingview_callback_port,
        interactive=interactive,
    )


def tradingview_token_status(settings) -> int:
    status = make_tradingview(settings).storage.status()
    print()
    for key, value in status.items():
        print(f"  {key:<14} {value}")
    print()
    return 0


def auth_tradingview(settings) -> int:
    client = make_tradingview(settings, interactive=True)
    tools = client.list_tools()
    print(f"\nSigned in. TradingView's MCP server offers {len(tools)} tools.")
    print(f"Tokens stored in {client.storage.path} (outside the project; never commit it).")
    print("Next: python run.py --tradingview-tools\n")
    return 0


def tradingview_diagnose(settings) -> int:
    from pipeline.providers.tradingview_mcp import diagnose

    print()
    print("\n".join(diagnose(settings.validate.tradingview_url)))
    print()
    return 0


def tradingview_probe(settings, url: str) -> int:
    from pipeline.providers.tradingview_mcp import probe_url

    print()
    print("\n".join(probe_url(url)))
    print()
    return 0


def tradingview_tools(settings) -> int:
    import json

    from pipeline.providers.tradingview_mcp import describe_tools

    tools = make_tradingview(settings).list_tools()
    print(f"\n{len(tools)} tools:\n")
    print(describe_tools(tools))
    target = settings.log_dir / "tradingview_tools.json"
    target.write_text(json.dumps([t.model_dump(mode="json") for t in tools], indent=2,
                                 ensure_ascii=False), encoding="utf-8")
    print(f"\nFull schemas saved to {target}\n")
    return 0


def tradingview_call(settings, spec: list[str]) -> int:
    from pipeline.providers.tradingview_mcp import describe_result, parse_tool_args

    from pipeline.providers.tradingview_data import RateLimited, tool_payload

    name, args = spec[0], parse_tool_args(spec[1:])
    client = make_tradingview(settings)
    print(f"\ncalling {name}({args})\n")
    for delay in (10, 30, None):
        result = client.call_tool(name, args)
        try:
            tool_payload(result, name)
        except RateLimited:
            if delay is not None:
                print(f"rate limited by TradingView (429) - retrying in {delay}s")
                time.sleep(delay)
                continue
        except Exception:  # noqa: BLE001 — show the raw result whatever the failure
            pass
        break
    print(describe_result(result))
    print()
    return 0


def discover_sec(settings, ticker: str) -> int:
    """Look at SEC's live answers before trusting the Form 4 mapping."""
    from collections import Counter
    from datetime import date, timedelta

    from pipeline.providers.sec_edgar import (
        SUBMISSIONS_URL, TICKERS_URL, SecClient, archive_url, cik_for, parse_form4, recent_filings)

    client = SecClient(settings.env("SEC_USER_AGENT"), settings.cache_dir / "sec")
    cik = cik_for(client.json(TICKERS_URL), ticker)
    submissions = client.json(SUBMISSIONS_URL.format(cik=cik))
    print(f"\n== SEC EDGAR: {ticker} -> CIK {cik} ({submissions.get('name', '?')})")
    recent = submissions.get("filings", {}).get("recent", {})
    print(f"   filings.recent columns: {sorted(recent)}")
    year = recent_filings(submissions, set(recent.get("form", [])), date.today() - timedelta(days=365))
    print("\n   form types filed in the last 365 days:")
    for form, count in Counter(f.form for f in year).most_common(20):
        print(f"     {form:<12} {count}")
    form4 = [f for f in year if f.form == "4"]
    if not form4:
        print("\n   no Form 4 in the last year\n")
        return 0
    first = form4[0]
    url = archive_url(cik, first)
    print(f"\n   newest Form 4: {first.accession} filed {first.filed}, "
          f"primaryDocument '{first.primary_document}'\n   raw XML: {url}")
    for row in parse_form4(client.fetch(url, cache=True), first.accession, expected_cik=cik)[:5]:
        print("     " + ", ".join(f"{k}={row[k]}" for k in
                                   ("insider", "role", "date", "code", "direction", "shares", "price",
                                    "plan_10b5_1")))
    print()
    return 0


def docs_spike(settings, dtype: str) -> int:
    """Run the phase-4 retrieval check and print recall with every question's top 5."""
    from pipeline.docs_spike import QwenVLEmbedder, run_spike

    input_dir = settings.root / "docs_input"
    embedder = QwenVLEmbedder(dtype=dtype)
    result = run_spike(input_dir, settings.cache_dir / "docs_spike", embedder)

    print(f"\n== Phase 4 spike — {result.model}")
    print(f"   {result.pages} pages, {result.seconds_per_page:.1f}s per page to embed "
          "(0 = all cached)\n")
    for row in result.rows:
        mark = "HIT " if row["rank"] and row["rank"] <= 5 else "MISS"
        print(f"  {mark} rank {row['rank'] or '-':>3}  {row['question']}")
        print(f"        expected {row['expected']}")
        for hit in row["top"]:
            print(f"          {hit}")
    print(f"\n  recall@1 {result.recall_at_1:.0%}   recall@5 {result.recall_at_5:.0%}   "
          f"MRR {result.mrr:.2f}\n")
    out = settings.log_dir / "docs_spike.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Details saved to {out} (stays on this machine).\n")
    return 0


def check_docs_model(settings, dtype: str) -> int:
    """Two generated pages with known content; a question must rank its page first."""
    import tempfile
    from pathlib import Path as _Path

    import numpy as np
    import pymupdf

    from pipeline.docs_spike import QwenVLEmbedder, render_pages

    pages_text = [
        "Quarterly report\nRevenue grew 20% to $30 billion.\nGross margin 72%.",
        "Fund prospectus\nManagement fee: 0.25% per year.\nCustodian: Example Bank.",
    ]
    with tempfile.TemporaryDirectory() as tmp:
        pdf = _Path(tmp) / "check.pdf"
        doc = pymupdf.open()
        for text in pages_text:
            doc.new_page(width=595, height=842).insert_text((60, 120), text, fontsize=22)
        doc.save(pdf)
        images = render_pages(pdf, _Path(tmp) / "pages")

        started = time.monotonic()
        model = QwenVLEmbedder(dtype=dtype)
        loaded = time.monotonic() - started
        print(f"\n  model loaded in {time.monotonic() - started:.0f}s; embedding 2 test pages "
              "(the first can take a minute on CPU)...", flush=True)
        started = time.monotonic()
        vecs = []
        for n, image in enumerate(images, 1):
            page_started = time.monotonic()
            vecs.append(model.embed_pages([image])[0])
            print(f"  page {n}/2 done in {time.monotonic() - page_started:.1f}s", flush=True)
        page_vecs = np.stack(vecs)
        per_page = (time.monotonic() - started) / len(images)
        queries = ["What is the management fee?", "How much did revenue grow?"]
        scores = model.embed_queries(queries) @ page_vecs.T

    try:
        import psutil
        memory = f"{psutil.Process().memory_info().rss / 2**30:.1f} GB"
    except ImportError:
        memory = "n/a"
    print(f"\n  model        {model.name}")
    print(f"  load time    {loaded:.0f}s   embedding {per_page:.1f}s per page   memory {memory}")
    ok = scores[0].argmax() == 1 and scores[1].argmax() == 0
    for q, row in zip(queries, scores):
        print(f"  {q:<30} page1 {row[0]:.3f}  page2 {row[1]:.3f}")
    if not ok:
        print("\n  The model loaded but matched the questions to the wrong pages. Send this output.\n")
        return 1
    print(f"\n  The model works. 20 PDFs of ~30 pages would take about "
          f"{600 * per_page / 60:.0f} minutes to embed once (cached afterwards).")
    print("  Next: put PDFs and questions.yaml in docs_input\\, then run --docs-spike\n")
    return 0


def seed_saved_answers(settings, ticker: str) -> int:
    """Turn earlier successful TradingView payloads into saved answers."""
    from datetime import datetime, timezone

    from pipeline.providers.tradingview_data import (
        EARNINGS_TOOL, SYMBOL_DATA_TOOL, earnings_from_payload, market_cap_from_payload)
    from pipeline.stages.validate import SAVED_ANSWERS, SavedAnswers

    symbol = settings.validate.tradingview_symbol(ticker)
    saved = SavedAnswers(settings.cache_dir / ticker / SAVED_ANSWERS)
    folder = settings.log_dir / "tradingview_payloads"
    seeded = 0
    for kind, tool, parse in (("market_cap", SYMBOL_DATA_TOOL, market_cap_from_payload),
                              ("next_earnings", EARNINGS_TOOL, earnings_from_payload)):
        path = folder / f"{tool}.json"
        if not path.exists():
            print(f"  {kind:<14} no file {path}")
            continue
        try:
            value = parse(json.loads(path.read_text(encoding="utf-8")), symbol)
        except (ValueError, PipelineError) as exc:
            print(f"  {kind:<14} not usable: {exc}")
            continue
        when = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        saved.put(symbol, kind, value, when)
        seeded += 1
        print(f"  {kind:<14} saved (answer from {when:%Y-%m-%d %H:%M} UTC)")
    print(f"\n{seeded} answer(s) saved to {saved.path}\n")
    return 0 if seeded else 1


def check_timesfm(settings) -> int:
    """Load the real model and forecast a series with a known answer."""
    import numpy as np

    from pipeline.forecasting import TimesFMForecaster, check_forecast, quantile_index

    f = settings.forecast
    print(f"\nLoading {f.timesfm_checkpoint} on {f.timesfm_device}. The first run downloads "
          "the weights (large); later runs load them from the local cache.\n")
    started = time.monotonic()
    model = TimesFMForecaster(f.timesfm_checkpoint, revision=f.timesfm_revision,
                              device=f.timesfm_device, batch_size=f.timesfm_batch_size)
    loaded = time.monotonic() - started

    # Weekly-period wave around 100: a model that works continues it.
    t = np.arange(260, dtype=float)
    series = 100 + 10 * np.sin(2 * np.pi * t / 5)
    started = time.monotonic()
    q = model.forecast([series], 10)[0]
    took = time.monotonic() - started
    check_forecast(q, float(series[-20:].mean()), nonnegative=True, context="check")
    median = q[:, quantile_index(model.quantiles, 0.5)]
    truth = 100 + 10 * np.sin(2 * np.pi * np.arange(260, 270) / 5)
    error = float(np.mean(np.abs(median - truth)))

    print(f"  model        {model.name}")
    print(f"  quantiles    {model.quantiles}")
    print(f"  load time    {loaded:.1f}s   forecast time {took:.1f}s")
    print(f"  expected     {' '.join(f'{v:6.1f}' for v in truth)}")
    print(f"  forecast     {' '.join(f'{v:6.1f}' for v in median)}")
    print(f"  mean error   {error:.2f} (the wave's amplitude is 10)\n")
    if error > 3:
        print("  The model loaded but did not follow a simple wave. Send this output.\n")
        return 1
    print("  TimesFM works. Next: python run.py --ticker NVDA --stages data,validate,forecast\n")
    return 0


def selftest(settings) -> int:
    """Prove the install works without a key or a network connection.

    Runs the real data stage against the synthetic provider in a throwaway cache
    directory, so a failure here is an installation problem, never a data one.
    """
    import tempfile
    from pathlib import Path as _Path

    from pipeline.cache import make_run_context
    from pipeline.gate import require_validation_pass
    from pipeline.stages.data import DataStage, print_close_preview
    from pipeline.stages.forecast import ForecastStage
    from pipeline.stages.validate import ValidateStage

    print("\nSelf-test: running the data, validate and forecast stages against synthetic "
          "data (no API key, no network, no model download).\n")

    with tempfile.TemporaryDirectory() as tmp:
        scratch = replace_cache_dir(settings, _Path(tmp))
        ctx = make_run_context(scratch, "SELFTEST", "1970-01-01")
        result = DataStage().run(ctx)

        expected = {"data_ohlcv", "data_options", "data_macro", "data_reference"}
        missing = expected - set(result.artifacts)
        if missing or result.status != "ok":
            print(f"  FAILED: status={result.status} missing={sorted(missing)}")
            return 1
        for artifact in sorted(expected - {"data_reference"}):
            rows = len(ctx.read_parquet(artifact))
            print(f"  ok  {artifact:<14} {rows} rows")

        ValidateStage().run(ctx)
        for check in require_validation_pass(ctx)["checks"]:
            print(f"  ok  validate: {check['name']:<14} {check['status']}")
        forecast = ForecastStage()
        forecast._gate(ctx)                       # the real gate, as in a run
        result = forecast.run(ctx)
        print(f"  ok  forecast: {len(ctx.read_parquet('forecast_timesfm'))} rows "
              f"({result.details['forecast']['model']} stand-in — it repeats the last value, so "
              "'did not beat the naive forecast' above is expected here)")
        print_close_preview(ctx, rows=3)

    print("Self-test passed. The install is sound — pandas, pyarrow, Parquet IO,\n"
          "contracts, the ordering/freshness guards, the validation gate, the forecast\n"
          "checks and the stage runner all work. To check the TimesFM model itself:\n"
          "  python run.py --check-timesfm\n"
          "Add LSE_API_KEY to .env, then run:  python run.py --ticker NVDA --stages data\n")
    return 0


def replace_cache_dir(settings, cache_dir):
    """Settings is frozen; build a copy using a scratch cache and the synthetic provider."""
    import dataclasses
    return dataclasses.replace(
        settings,
        cache_dir=cache_dir,
        # No fallbacks: the self-test must never touch the network.
        data=dataclasses.replace(settings.data, provider="synthetic", fallback_provider=None,
                                 macro_fallback=None),
        validate=dataclasses.replace(settings.validate, provider="synthetic"),
        forecast=dataclasses.replace(settings.forecast, timesfm_provider="synthetic"),
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    from pathlib import Path
    settings = load_settings(Path(args.config) if args.config else None)
    setup_logging(settings.log_dir, verbose=args.verbose)

    if args.selftest:
        return selftest(settings)

    if args.discover_sec:
        try:
            return discover_sec(settings, args.discover_sec.strip().upper())
        except PipelineError as exc:
            log.error("%s", exc)
            return 1

    if args.check_docs_model:
        try:
            return check_docs_model(settings, args.docs_dtype)
        except PipelineError as exc:
            log.error("%s", exc)
            return 1

    if args.docs_spike:
        try:
            return docs_spike(settings, args.docs_dtype)
        except PipelineError as exc:
            log.error("%s", exc)
            return 1

    if args.seed_saved_answers:
        return seed_saved_answers(settings, args.seed_saved_answers.strip().upper())

    if args.check_timesfm:
        try:
            return check_timesfm(settings)
        except PipelineError as exc:
            log.error("%s", exc)
            return 1

    if args.discover_session:
        try:
            return discover_session(settings, args.discover_session[0].strip().upper(),
                                    args.discover_session[1])
        except PipelineError as exc:
            log.error("%s", exc)
            return 1

    if args.discover_fundamentals:
        return discover_fundamentals(settings, args.discover_fundamentals.strip().upper())

    tradingview_commands = (
        (args.auth_tradingview, lambda: auth_tradingview(settings)),
        (args.tradingview_diagnose, lambda: tradingview_diagnose(settings)),
        (args.tradingview_probe, lambda: tradingview_probe(settings, args.tradingview_probe)),
        (args.tradingview_tools, lambda: tradingview_tools(settings)),
        (args.tradingview_call, lambda: tradingview_call(settings, args.tradingview_call)),
        (args.tradingview_token_status, lambda: tradingview_token_status(settings)),
    )
    for requested, command in tradingview_commands:
        if requested:
            try:
                return command()
            except PipelineError as exc:
                log.error("%s", exc)
                return 1

    if args.discover_macro is not None:
        return discover_macro(settings, args.discover_macro)

    if not args.ticker:
        log.error("--ticker is required (or use --discover-macro)")
        return 2

    try:
        stage_names = resolve_stages(args.stages)
    except ValueError as exc:
        log.error("%s", exc)
        return 2

    ctx = make_run_context(settings, args.ticker, args.run_date)
    registry = build_registry()
    log.info("run %s %s -> %s", ctx.ticker, ctx.run_date, ", ".join(stage_names))
    log.info("artifacts: %s", ctx.run_dir)

    exit_code = 0
    for name in stage_names:
        stage = registry[name]
        try:
            stage._gate(ctx)
            result = stage.run(ctx)
        except PipelineHalt as exc:
            log.error("HALT at '%s': %s", name, exc)
            ctx.record_stage(name, "halted", error=str(exc))
            return 3
        except PipelineError as exc:
            log.error("stage '%s' failed: %s", name, exc)
            ctx.record_stage(name, "failed", error=str(exc))
            return 1
        except Exception as exc:  # noqa: BLE001 — unexpected, log with traceback
            log.exception("stage '%s' crashed: %s", name, exc)
            ctx.record_stage(name, "crashed", error=repr(exc))
            return 1

        ctx.record_stage(name, result.status, summary=result.summary, **result.details)
        if result.status == "not_implemented":
            log.info("- %s: %s", name, result.summary)
        else:
            log.info("+ %s: %s", name, result.summary or result.status)

    # Phase-1 acceptance aid: show the closes so they can be eyeballed.
    if "data" in stage_names and ctx.exists("data_ohlcv"):
        from pipeline.stages.data import print_close_preview
        print_close_preview(ctx)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
