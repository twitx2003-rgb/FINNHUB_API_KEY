# Licences and usage limits

This project is for **personal, non-commercial research**. Several components below
forbid redistribution or commercial use, and at least one forbids production use.
Read this before repurposing anything here.

Verified 2026-09-22 against PyPI/repository metadata.

## Tools in the pipeline

| Tool | Code licence | Data / weights licence | Practical limits |
|---|---|---|---|
| **lse-data** 0.14.0 | MIT | **Separate and stricter** — see below | Free key. Streaming and vault downloads share one allowance; check `GET /vault/usage`. `limit` is clamped to 5000 rows per call. |
| **TradingView MCP** | n/a (hosted service) | TradingView Terms of Service | **Requires a paid plan (Essential or higher).** Public beta — tool names and schemas may change. OAuth 2.1, no API key. |
| **mcp** (Python SDK) 2.2 | MIT | n/a | Client library only; TradingView's terms govern the data it returns. |
| **Finnhub** (optional validator) | n/a (hosted) | **Free tier is non-commercial** | 60 API calls/minute. Monetising or redistributing the data requires a paid plan. |
| **Scrapling** 0.4.15 | BSD-3-Clause | n/a | You remain responsible for the target site's ToS and robots.txt. Scraping a site does not grant rights to its content. |
| **PixelRAG** 0.4.0 | Apache-2.0 | Model weights are third-party — see below | Requires Python ≥3.12. |
| **TimesFM** 3.0.2 | Apache-2.0 (code) | **`timesfm-non-commercial-license-v1.0`** for 3.0 weights | "Non-commercial, non-production" only. Fine for this project. TimesFM ≤2.5 weights are Apache-2.0 if you ever need commercial use. |
| **Kronos** | MIT | Weights on Hugging Face (`NeoQuasar/Kronos-*`) | Authors state the reference pipeline is "a simplified example and not a production-ready quantitative trading system". |
| **TradingAgents** 0.3.1 | Apache-2.0 | n/a | Research framework. Not investment advice. |
| **Anthropic API** | n/a | Anthropic Commercial Terms | Billed per token. Costs money on every debate run. |

## lse-data — the important one

The Python client is MIT, but **that licence confers no rights in the data**. The data
terms state:

> Data retrieved with an LSE key may be used for your own research, trading and model
> training, including for commercial purposes. It may not be redistributed, resold, or
> otherwise made available to third parties, whether in bulk or by any competing feed,
> download service or interface sourced from LSE.

For this project that means: analysing it locally is fine; **publishing the contents of
`cache/` — or a report that reproduces the raw series — is not.** `cache/` is gitignored
for exactly this reason.

## Model weights still to confirm

I could not reach huggingface.co from the build environment, so these model-card
licences are **unverified** and must be checked on first download:

- `Qwen/Qwen3-VL-Embedding-2B` (PixelRAG base embedder)
- `Chrisyichuan/wiki-screenshot-embedding-lora` (PixelRAG retrieval adapter)
- `NeoQuasar/Kronos-small` / `Kronos-base` and their tokenizers

## Not financial advice

Every number this pipeline produces — forecasts especially — is model output over
historical data. Forecast stages are deliberately constrained: TimesFM is restricted to
seasonal/trend series (volume, CPI, rates), and no single-stock price output is ever
labelled a prediction. Treat the debate stage as two arguments, not a recommendation.
