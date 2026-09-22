# Personal capital-market research pipeline

A staged research pipeline where **each stage only receives input that already passed
validation**. Personal, non-commercial use — see [LICENSES.md](LICENSES.md).

```
data → validate → extract → docs → forecast → debate → report
         │
         └── a failure here HALTS everything downstream
```

Each stage writes Parquet to `cache/<TICKER>/<RUN_DATE>/` and reads the previous stage's
output, so any stage can be rerun on its own.

## Status

| Phase | Stage(s) | State |
|---|---|---|
| 1 | `data` | **done** — accepted against live data |
| 2 | `validate` | **in progress** — TradingView connection built, mapping pending |
| 3 | `forecast` (TimesFM) | planned |
| 4 | `docs`, `extract` | planned |
| 5 | `forecast` (Kronos), `debate`, `report` | planned |

Unbuilt stages are registered placeholders, so `--stages all` already runs the whole
sequence and reports what is not implemented yet.

## Setup (Windows)

Uses **Python 3.14** (verified on 3.14.4: every dependency has a Windows cp314 wheel, and
the full suite passes). TradingAgents, which arrives in phase 5, targets 3.12; if it does
not run on 3.14, that stage gets a private uv-managed 3.12 venv rather than a change to the
system Python.

```bat
py -3.14 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt

```

Now verify the install before you have any credentials:

```bat
python -m pytest -q          :: 72 offline tests
python run.py --selftest     :: runs the real data stage against synthetic data
```

Both are network-free and key-free. If they pass, the installation is sound, so any
later failure is a data or credential problem rather than a broken environment.

Then add your key:

```bat
copy .env.example .env
notepad .env
```

Put your free London Strategic Edge key (https://londonstrategicedge.com/data) in
`LSE_API_KEY`. Nothing else is needed for phase 1.

## Run

```bat
python run.py --ticker NVDA --stages data
```

This prints the last five daily bars and the latest close, so you can compare it against
a number you already know.

```bat
python run.py --ticker NVDA --stages all        :: full sequence
python run.py --ticker NVDA --stages data,validate
python run.py --discover-macro cpi              :: search macro series codes for config.yaml
python run.py --selftest                        :: verify the install, no key or network
python run.py --auth-tradingview                :: one-time TradingView sign-in (browser)
python run.py --tradingview-tools               :: list TradingView MCP tools
python -m pytest -q                             :: offline tests, no network
```

Exit codes: `0` ok, `1` stage failed, `2` bad arguments, `3` validation HALT.

## Verified dependency versions

The dependency set was resolved and the full test suite run on **Python 3.12.3 and 3.14.4** with:

```
pandas 3.0.6   pyarrow 25.0.1   numpy 2.5.3   lse-data 0.14.0
yfinance 1.7.0   pandas-datareader 0.11.1   PyYAML 6.0.3   pytest 9.1.1
```

`requirements.txt` deliberately uses lower bounds rather than a lockfile: the versions
above were resolved on Linux, and pinning them exactly could select a build with no
Windows wheel. If you want reproducibility on your machine, run
`pip freeze > requirements.lock.txt` once your install is working.

## Configuration

`config.yaml` holds behaviour, `.env` holds secrets (gitignored). Unknown keys in
`config.yaml` are rejected at startup rather than silently ignored.

## The lse-data ordering trap

`lse-data`'s `candles()`, `economics()` and `bond_yields()` default to **`order="asc"`** —
oldest first — so a caller who omits the argument silently analyses the *start* of history
(US equities go back to 2003). Confusingly, `dividends()`, `splits()`, `insider_trades()`
and `options_flow()` default to `"desc"` instead, so there is no single default to
remember.

Three defences, all in `pipeline/providers/`:

1. `order="desc"` is passed **explicitly** on every call.
2. `assert_descending()` checks the *response* really is newest-first — so a server-side
   default change is caught rather than trusted.
3. `assert_fresh()` fails if the newest row is older than `data.max_staleness_days`.

A wrong-order response therefore fails loudly instead of producing a confident forecast
of 2003.

## Design notes

- **Never guess a field.** Provider responses go through `pick()`, which raises and lists
  the keys actually present rather than falling back to a default. A guessed field is how
  a strike ends up reported as a price.
- **The HALT rule is structural.** Stages call `require_validation_pass()` before running,
  so the check cannot be forgotten and `--stages report` on a failed run refuses to start.
- **Bar sanity is shared.** `assert_ohlcv_sane()` enforces `high >= max(open,close)`,
  `low <= min(open,close)` and `volume >= 0` on vendor data now, and will validate
  generated Kronos candles in phase 5.
