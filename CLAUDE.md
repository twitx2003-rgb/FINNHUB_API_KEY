# CLAUDE.md — project brief for Claude Code

Personal capital-market research pipeline. **Personal, non-commercial use only** — see
`LICENSES.md` before reusing anything. Built phase by phase; the owner reviews each phase
before the next one starts.

**Talk to the user in Hebrew.** Code, comments and commit messages stay in English.

## Environment (the user's machine)

- Windows. Project lives at `C:\dev\market-research-pipeline`.
- **Python 3.14.4**, venv at `.venv`, created with `py -3.14 -m venv .venv`.
- Do **not** use Python 3.12: the Microsoft Store 3.12 on this machine is broken
  (`0x80070780`), and the user does not want system Python installs changed.
- Call the venv interpreter directly — no activation needed, works in cmd and PowerShell:
  `.venv\Scripts\python.exe ...`
- VS Code's integrated terminal is PowerShell: use `$env:USERPROFILE`, not `%USERPROFILE%`.
- Code lives on GitHub as branch **`market-research-pipeline`** of
  `twitx2003-rgb/FINNHUB_API_KEY`. **That repo is public** — the user chose this knowing
  it. So: never commit vendor data (LSE/Yahoo/FRED values), not even a few numbers in
  notes, commit messages or test fixtures; use synthetic values. `.env` and `cache/`
  stay gitignored. The repo's default branch is an unrelated project.

## Commands

```
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pytest -q                     # 79 offline tests (1 skips without IPv6)
.venv\Scripts\python.exe run.py --selftest                # install check, no key/network
.venv\Scripts\python.exe run.py --ticker NVDA --stages data
.venv\Scripts\python.exe run.py --ticker NVDA --stages all
.venv\Scripts\python.exe run.py --discover-macro cpi      # search LSE macro catalogue
.venv\Scripts\python.exe run.py --discover-fundamentals NVDA   # raw market-cap / earnings fields
.venv\Scripts\python.exe run.py --tradingview-diagnose  # which OAuth routes the server offers
.venv\Scripts\python.exe run.py --auth-tradingview      # one-time browser sign-in
.venv\Scripts\python.exe run.py --tradingview-tools     # list TradingView MCP tools
.venv\Scripts\python.exe run.py --tradingview-call TOOL key=value ...   # raw tool result
.venv\Scripts\python.exe run.py --tradingview-token-status  # stored sign-in: expiry, refresh token (no secrets)
.venv\Scripts\python.exe run.py --discover-session NVDA 2026-09-16  # is LSE's daily bar regular or extended hours
```

Exit codes: 0 ok, 1 stage failed, 2 bad args, 3 validation HALT.

## Architecture rules — keep these

- Stages: `data → validate → extract → docs → forecast → debate → report`
  (`pipeline/stages/__init__.py`, `STAGE_ORDER`). Unbuilt stages are registered
  placeholders (`NotImplementedStage`).
- Stages never pass data in memory. Each writes Parquet/JSON to
  `cache/<TICKER>/<RUN_DATE>/` via `RunContext` (`pipeline/cache.py`) and reads the
  previous stage's artifact, so any stage can be rerun alone.
- **HALT is structural:** every stage after `data` has `requires_validation = True`, and
  the runner calls `gate.require_validation_pass()` first. Never bypass it.
- Every artifact is checked against a contract in `pipeline/contracts.py` before writing.
- **Never guess a field.** Provider rows go through `pick()` in
  `pipeline/providers/base.py`, which raises and lists the keys actually present.
  A guessed field is how a strike gets reported as a price.
- Config in `config.yaml` (unknown keys are rejected), secrets in `.env` (gitignored).
- Tests are offline. `SyntheticProvider` (`pipeline/providers/synthetic.py`) is shared by
  the tests and `--selftest`; it ends on yesterday so results never depend on market hours.
- A bar is only a close once its session has ended — see `pipeline/market_hours.py`.

## Verified facts — do not re-derive from docs

`lse-data` 0.14.0, confirmed by introspecting the installed package (the import name is
**`lse`**, not `lse_data`):

- `LSE(api_key=None, url=..., timeout=60)`; reads `LSE_API_KEY`; errors are `LSEError`.
- `candles(symbol, timeframe="1m", start, end, limit<=5000, order="asc", dataset)`
- `options(underlying, type, expiry, strike, min_dte, max_dte, limit)` — `underlying=`, no `order`.
- `economics(symbol, start, end, order="asc", limit)`; `bond_yields(..., order="asc")`.
- Returns `List[dict]`; candles use key `timestamp`. Server clamps `limit` to 5000.
- **The ordering trap:** `candles`/`economics`/`bond_yields`/`series` default to
  `order="asc"` (oldest first — US equities go back to 2003), while `dividends`/`splits`/
  `insider_trades`/`options_flow` default to `"desc"`. Always pass `order` explicitly,
  then `assert_descending()` on the response and `assert_fresh()` on the frame.

Rule for every new library: install it and **introspect the real signatures** before
writing code against it. Do not trust README summaries or memory.

## Status

- **Phase 1 (data stage): ACCEPTED 2026-09-22.** On the user's Windows machine, LSE's
  latest NVDA close matched the close the user knew to within a few cents. 59 offline
  tests pass; `--selftest` passes on Python 3.14.4.
- What the live runs established (details in `pipeline/` docstrings and tests):
  - **Mid-session bars:** both providers return today's unfinished bar. It is dropped until
    `data.session_close` (`pipeline/market_hours.py`).
  - **LSE option chain:** the live key set is listed in `pipeline/providers/lse.py`. Volume
    is `volume_today` (a `volume` lookup with a default of 0 would have marked every
    contract untraded). Untraded contracts have null IV/greeks; `pick(allow_null=True)`
    keeps those as NaN. LSE seems to list only contracts that traded that day.
  - **5000-row cap:** the NVDA chain exceeds it; `_chain_rows()` pages by DTE window
    after probing that the bounds are inclusive (they are). A newest-first series that hits
    the cap only loses its oldest history.
  - **IV scale:** LSE reports IV as a fraction (ATM ~30-day came back around 0.4). The
    chain-wide median is much higher, driven by short-dated far-OTM contracts.
  - **Macro:** US CPI is `usacsa` (seasonally adjusted, same definition as FRED CPIAUCSL).
    LSE's daily US10Y lagged the run date by 12 days, so every macro series is now checked
    against a per-frequency age limit and, if stale, replaced by its FRED copy when that
    copy is fresher (`pipeline/macro_freshness.py`; user's decision).
- **Open data question (not blocking):** LSE and Yahoo disagree on some NVDA daily bars.
  LSE volume ran ~25-30% below Yahoo on every day checked and missed the 2026-09-18
  quarterly-expiration spike; one close differed by ~0.8%. Plausibly LSE's bars exclude
  the closing auction and some venues — unconfirmed. Stage 2 cross-checks every run
  against TradingView, which will settle the close question; which source feeds the
  phase 3 volume forecast is decided then (consider cross-checking volume in stage 2).
- **Phase 2 (validate): IN PROGRESS.** The user has a TradingView plan (Essential+).
  - Built: `pipeline/providers/tradingview_mcp.py` — OAuth 2.1 client written against the
    introspected mcp 2.2.0 API, tokens in `~/.mrp/tv_tokens.json` (outside the repo),
    one-shot loopback callback on localhost:8765, and headless runs that raise
    `AuthorizationRequired` instead of waiting for a browser. Tested end to end against a
    local OAuth-protected MCP server (`tests/test_tradingview_mcp.py`): dynamic client
    registration, PKCE, token storage, headless reuse.
  - **LSE has no earnings dates at all** (no mention anywhere in the client). The primary
    next-earnings date comes from Yahoo (`Ticker.calendar["Earnings Date"]`, a list — two
    dates mean an unconfirmed window). Market cap comes from LSE `fundamentals()`.
  - **First live sign-in failed, cause found:** TradingView *does* support dynamic client
    registration (`--tradingview-diagnose`: AS `https://www.tradingview.com`, registration
    at `/mcp/oauth/register`, PKCE S256, scopes `mcp:read mcp:tools`, no client ID metadata
    documents). But its sign-in host is behind bot protection: the mcp SDK builds OAuth
    discovery/registration requests as bare `Request`s with no User-Agent/Accept, those got
    403, and the SDK then guessed `/register` on the MCP host and got 404. Fix: an httpx2
    request hook (`_identify_request`) sets User-Agent + Accept on every request, including
    the OAuth flow's own. `--tradingview-diagnose` now sends the metadata request both ways
    and prints both statuses. Tests reproduce the block with a stand-in.
  - **Second live sign-in:** discovery and registration passed; the browser then showed a
    CloudFront 403 on the authorize page. The one thing that differed from known-good MCP
    clients was the redirect URI `http://127.0.0.1:8765/callback` (an IP literal in a query
    parameter is a classic CDN/WAF block pattern). Now `http://localhost:8765/callback`,
    with the callback listening on both 127.0.0.1 and ::1 (Windows may resolve localhost
    to IPv6 first). A stored client registered with a different redirect is dropped so the
    SDK re-registers. `--tradingview-diagnose` probes the authorize endpoint with both
    redirect hosts and reports which one the CDN blocks — confirm the cause there.
  - **Third live sign-in: the localhost hypothesis did not fix it.** Registration with the
    localhost redirect succeeded (201), but the browser still got the CloudFront 403
    *immediately* on the real authorize URL (user confirmed: before any sign-in page).
    Scripted authorize requests with a placeholder client_id returned 400 — but the
    probe then counted only 403s as CDN blocks, and CloudFront also generates 400 error
    pages, so "the script passes the CDN" was NOT established. Detection now keys on
    CloudFront's own error-page signals (`Generated by cloudfront` / `X-Cache: Error from
    cloudfront`), never on `x-amz-cf-id` (present on every CloudFront response), and the
    probes print a page snippet showing who answered.
    `run.py --tradingview-probe URL` follows the real URL hop by hop without cookies and
    names the hop the CDN blocks. Next evidence: that output, an InPrivate-window attempt,
    and whether the block appears before or after signing in/approving.
  - **Sign-in SOLVED (fourth attempt).** The probe showed authorize -> 302 (client and
    params accepted) and the CloudFront block on the *redirect target*
    `/accounts/signin/` (hop 1). Signing in on tradingview.com first, in the default
    browser, skips that redirect; `--auth-tradingview` then reached the approval screen
    and completed. The sign-in prompt now says to sign in on the main site first.
  - **Live provider shapes (from `--discover-fundamentals NVDA`):**
    - LSE `fundamentals()`: keys `beta country currency current_price description
      dividend_yield exchange industry ipo_date logo_url market_cap name pe_ratio
      profit_margin revenue_ttm sector symbol updated_at website week_52_high week_52_low`.
      `market_cap` is in raw USD (not millions). It is a snapshot: `current_price` was the
      previous session's close, and `week_52_high` was *below* `current_price` — some fields
      are stale. So compare market cap at a common price (or compare implied shares), not
      raw values from different days.
    - Yahoo `calendar`: `Earnings Date` came back as a single date (confirmed date); also
      EPS/revenue estimate ranges and dividend dates.
  - **Tools discovered (35, names `mcp-tv-*` / `mcp-watchlist-*`; full schemas in the
    user's `logs/tradingview_tools.json`).** All symbols are `EXCHANGE:TICKER`
    (`NASDAQ:NVDA`). For stage 2:
    - close: `mcp-tv-get-ohlcv(symbol, interval="1D", count<=5000, summary)`
    - market cap: `mcp-tv-get-symbol-data(symbol, columns[])` — screener columns, catalog
      via `mcp-tv-get-screener-columns(market, group, search)`
    - next earnings: `mcp-tv-get-earnings-calendar(symbols[], date_from, date_to)`
    - also useful later: `get-financials`, `get-financial-history`, `get-forecasts`,
      `get-economic-data` (ECONOMICS:<CC><IND>), `get-documents`/`get-document-view`
      (10-K/10-Q/transcripts — candidate input for phase 4), `get-news` (has `lang=he`).
    - The same server can create/delete alerts and edit watchlists. `call_tool` refuses
      anything that is not get/list/search/run-screener (`is_read_only`). Keep it that way.
  - **Live output shapes (first calls):**
    - Failures are NOT flagged by MCP: `is_error` is False and the payload is
      `{"success": false, "error": "..."}`. `tradingview_data.tool_payload` turns that
      into `ToolFailed` / `RateLimited`; never read results without it.
    - `get-ohlcv`: `{success, symbol, interval, count, notice, summary{...}, bars:[{t,o,h,l,c,v}]}`,
      bars oldest first, `t` = unix seconds at the session open (13:30 UTC), newest bar can
      be today's live session (the notice says so) -> `bars_frame` + `drop_incomplete_session`.
    - `get-symbol-data` and `get-earnings-calendar` both returned
      `tradingview api: https://scanner.tradingview.com/america/scan: 429` (rate limit behind
      the MCP server). `TradingViewData.fetch` retries 429 only (5/15/45s); the raw
      `--tradingview-call` retries too (10/30s). Their success shapes are still unknown.
  - The scanner stayed at 429 across retries (10/30s) and for `get-financials` too.
    **User decision: wait for TradingView** (no Finnhub fallback for now).
  - **Built: the validate stage** (`pipeline/stages/validate.py`). Checks, each
    pass / fail / unverifiable; anything but pass -> `validation.json` status fail and
    `PipelineHalt` (exit 3); the gate lists non-passing checks.
    - `last_close`: our newest completed close vs TradingView's close that day
      (TV bars go through `drop_incomplete_session` too). Fails if TV has completed
      sessions newer than ours ("rerun data") or no bar for our day.
    - `market_cap`: implied shares (cap / price it was computed at) on both sides.
    - `next_earnings`: TV date inside our Yahoo date/window +- `earnings_tolerance_days`.
    - Data stage now writes `data_reference.json` via provider `market_cap_snapshot` /
      `next_earnings` (LSE fundamentals, Yahoo calendar; failures recorded as `error`,
      turned into unverifiable by stage 2).
    - `TradingViewData.market_cap` / `.next_earnings` call the real tools but raise
      `ShapeNotMapped` on success and save the payload to
      `logs/tradingview_payloads/<tool>.json` — the success format is unknown, so the
      validate output tells the user to send that file. Map it then; do not guess.
    - `validate.provider: synthetic` (`SyntheticSource`) is used by `--selftest`, which
      now runs data + validate and opens the gate.
  - **First live `data,validate` run: all three checks unverifiable — 401, then the SDK
    asked for a browser sign-in.** Cause (SDK gap, mcp 2.2): `_initialize` loads stored
    tokens but not their expiry, so an expired access token counts as valid, is sent,
    gets 401, and the SDK goes to full re-authorization without trying the refresh
    token. Fix: `FileTokenStorage` records `tokens_saved_at` (legacy files: file mtime)
    and `_StoredExpiryAuth._initialize` restores `token_expiry_time`, so the SDK
    refreshes first. Tests reproduce it against a fake server whose old tokens stop
    working (they fail with the plain provider). Headless sign-in errors now say why
    (no refresh token / refresh refused / rejected) and the SDK's traceback for that
    case is filtered. `run.py --tradingview-token-status` shows the stored state, no
    secrets. TradingView does issue refresh tokens; access tokens last **900 s**.
  - **Second live run: refresh went to `https://mcp.tradingview.com/token` -> 404.**
    Second SDK gap: a refresh before any 401 runs before metadata discovery, so the SDK
    falls back to `<MCP host>/token`. Real endpoint: `www.tradingview.com/mcp/oauth/token`.
    Fix: `_StoredExpiryAuth._discover_for_refresh` runs the SDK's own PRM + AS metadata
    discovery (with our User-Agent) whenever a refresh is about to happen. The fake
    server's token endpoint moved to `/oauth/token` so tests can't pass by the guess.
  - **Scanner answered; shapes mapped** (`market_cap_from_payload`,
    `earnings_from_payload`): symbol data `{"success", "data": {"close",
    "market_cap_basic"}}`; earnings `{"success", "data": {"count", "from", "to",
    "earnings": [{"symbol", "release_date", "release_next_date", ...}]}}` (row picked by
    exact symbol, exactly one). A payload that no longer parses is saved to
    `logs/tradingview_payloads/` and raised as `ShapeNotMapped`. TradingView's next
    date for the test ticker fell on a Saturday -> `check_earnings` flags weekend dates
    as likely estimates.
  - **Open data question: what LSE's daily close is.** `validation.json` now lists every
    overlapping day. LSE vs TradingView (regular session only, per its own notice):
    most days within a few hundredths of a percent, some past days ~0.5-0.8% apart,
    and LSE's bar for a finished day kept changing through the evening. Hypothesis:
    LSE daily bars include extended hours (close = last after-hours trade; would also
    explain the LSE-vs-Yahoo volume gap). Test: `run.py --discover-session NVDA DATE`
    rebuilds a day from LSE 5-minute candles (`LSEProvider.intraday`,
    `pipeline/session_check.py`) and says which close/volume the daily bar matches.
    If confirmed, decide with the user: regular-session bars built from LSE intraday,
    or another OHLCV source. Until then `session_close: 16:15` may be too early for LSE.
  - **Next:** once the scanner answers, map the two payloads -> Phase 2 done ->
    stop for review. Earlier line kept for history: `--tradingview-tools` (tool names and schemas are unknown — public beta).
  - Then build: market cap + earnings dates into the data stage (`data_reference.json`),
    the validate stage (close / market cap / next earnings vs TradingView, tolerances from
    `config.yaml`, write `validation.json`, raise `PipelineHalt` on any failed or
    unverifiable check).

## Roadmap (one phase at a time, stop for review after each)

**Phase 2 — validate.** Second source is the **official TradingView MCP**
(`https://mcp.tradingview.com/mcp`, streamable HTTP, **OAuth 2.1**, needs a paid
TradingView plan, public beta). User chose it over Finnhub.
- Python SDK: `mcp.client.auth.OAuthClientProvider` + a **file-backed `TokenStorage`**
  (`~/.mrp/tv_tokens.json`). One interactive `run.py --auth-tradingview`, then headless
  refresh. Pin the `mcp` version and read its source for the exact call shape.
- Call `list_tools()` at runtime, validate response shapes, HALT on anything unexpected.
- Compare last close / market cap / next earnings date within `validate.*` tolerances in
  `config.yaml`; write `validation.json` `{"status": "pass"|"fail", "checks": [...]}`.
- Keep the validator pluggable; Finnhub (`FINNHUB_API_KEY`) is the drop-in fallback.

**Phase 3 — forecast (TimesFM on volume).** `timesfm[torch]` 3.0.x. The **3.0 weights are
non-commercial** (`timesfm-non-commercial-license-v1.0`) — fine here, pinned in config.
Only seasonal/trend series (volume, CPI, rates). Backtest one horizon.

**Phase 4 — docs + extract.**
- Starts with a **PixelRAG Hebrew spike** before any integration: index ~20 real Hebrew
  PDFs, 10 known-answer questions, measure recall@5. Its LoRA was trained on English
  Wikipedia screenshots. If recall is poor: base Qwen3-VL-Embedding without the LoRA,
  then `tsystems/colqwen2.5-3b-multilingual-v1.0`. Same stage interface either way.
- PixelRAG runs in a **separate venv** out of process (`config.yaml` → `venvs`).
- Scrapling (`scrapling[fetchers]`) for pages without an API. Below the similarity
  threshold it must fail loudly, never return a guessed value.

**Phase 5 — Kronos + debate + report.**
- Kronos on CPU. Validate every generated candle with `assert_ohlcv_sane()` rules; drop
  and count invalid ones, flag the forecast degraded above 20%. Never present a
  single-stock price forecast as a prediction — call them scenario ranges.
- TradingAgents with `llm_provider="anthropic"`, key `ANTHROPIC_API_KEY` from `.env`.
  `config["data_vendors"]` points at a local **vendor shim** serving only validated
  `cache/` Parquet, so no unvalidated fetch reaches the debate. Confirm the config keys
  against the installed package. TradingAgents targets Python 3.12: first test it on
  3.14; if it fails, use a uv-managed private 3.12 for that venv only — never change the
  system Python.
- Hebrew HTML report, `dir="rtl"`: both theses, forecast ranges, a data-validation section.

## Open decisions

- Unverified model-card licences: `Qwen/Qwen3-VL-Embedding-2B`,
  `Chrisyichuan/wiki-screenshot-embedding-lora`, `NeoQuasar/Kronos-*`. Check on first
  download and record in `LICENSES.md`.

## Safety

- Never print, log or commit `.env` contents or API keys.
- Never publish `cache/` — LSE data may not be redistributed. The GitHub repo is public.
- Stages that call paid APIs (Anthropic in phase 5) cost money per run; say so before
  running them.
