#!/usr/bin/env python
"""Orchestrator.

    python run.py --ticker NVDA --stages all
    python run.py --ticker NVDA --stages data,validate
    python run.py --discover-macro
"""
from __future__ import annotations

import argparse
import logging
import sys

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

    name, args = spec[0], parse_tool_args(spec[1:])
    print(f"\ncalling {name}({args})\n")
    print(describe_result(make_tradingview(settings).call_tool(name, args)))
    print()
    return 0


def selftest(settings) -> int:
    """Prove the install works without a key or a network connection.

    Runs the real data stage against the synthetic provider in a throwaway cache
    directory, so a failure here is an installation problem, never a data one.
    """
    import tempfile
    from pathlib import Path as _Path

    from pipeline.cache import make_run_context
    from pipeline.stages.data import DataStage, print_close_preview

    print("\nSelf-test: running the data stage against synthetic data "
          "(no API key, no network).\n")

    with tempfile.TemporaryDirectory() as tmp:
        scratch = replace_cache_dir(settings, _Path(tmp))
        ctx = make_run_context(scratch, "SELFTEST", "1970-01-01")
        result = DataStage().run(ctx)

        expected = {"data_ohlcv", "data_options", "data_macro"}
        missing = expected - set(result.artifacts)
        if missing or result.status != "ok":
            print(f"  FAILED: status={result.status} missing={sorted(missing)}")
            return 1
        for artifact in sorted(expected):
            rows = len(ctx.read_parquet(artifact))
            print(f"  ok  {artifact:<14} {rows} rows")
        print_close_preview(ctx, rows=3)

    print("Self-test passed. The install is sound — pandas, pyarrow, Parquet IO,\n"
          "contracts, the ordering/freshness guards and the stage runner all work.\n"
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
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    from pathlib import Path
    settings = load_settings(Path(args.config) if args.config else None)
    setup_logging(settings.log_dir, verbose=args.verbose)

    if args.selftest:
        return selftest(settings)

    if args.discover_fundamentals:
        return discover_fundamentals(settings, args.discover_fundamentals.strip().upper())

    tradingview_commands = (
        (args.auth_tradingview, lambda: auth_tradingview(settings)),
        (args.tradingview_diagnose, lambda: tradingview_diagnose(settings)),
        (args.tradingview_probe, lambda: tradingview_probe(settings, args.tradingview_probe)),
        (args.tradingview_tools, lambda: tradingview_tools(settings)),
        (args.tradingview_call, lambda: tradingview_call(settings, args.tradingview_call)),
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
