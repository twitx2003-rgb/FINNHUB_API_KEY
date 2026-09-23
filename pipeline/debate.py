"""Bull vs bear debate over the validated brief, then a moderator's summary (phase 5).

User decisions (2026-09-23): our own debate instead of TradingAgents — 0.7.0 fetches
its own data (Yahoo / Google News) with no way to point it at validated artifacts,
and cannot write Hebrew; and it runs through Claude Code on the user's subscription
(`ClaudeCodeLLM`) rather than the paid API (`AnthropicLLM`, kept as an option).

How it stays honest:
- the model sees only the brief (pipeline/brief.py) — no tools, no web;
- every argument cites fact keys; `ground()` drops arguments with no citation or
  with a key that is not in the brief, and counts them;
- the instructions forbid recommendations, price targets, calling the Kronos range
  a prediction, and stating an unconfirmed earnings date as fact;
- output is JSON constrained by a schema (structured outputs), then validated here.

Anthropic API, as documented for the installed anthropic 1.8.0 (signature checked):
    client.beta.messages.create(model, max_tokens, system, messages, thinking,
        output_config={"effort": ..., "format": {"type": "json_schema", "schema": ...}},
        betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    stop_reason "refusal" / "max_tokens" are checked before reading content.
The API path costs money on every run; the estimate uses the requested model's list price.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Protocol

from .errors import ConfigError, ProviderError

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"

# USD per million tokens (input, output), first-party list prices; estimate only.
PRICES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

ARGUMENT = {
    "type": "object",
    "properties": {
        "claim": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["claim", "evidence"],
    "additionalProperties": False,
}
STATEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "thesis": {"type": "string"},
        "arguments": {"type": "array", "items": ARGUMENT},
        "rebuttals": {"type": "array", "items": ARGUMENT},
        "weak_points_of_own_case": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["thesis", "arguments", "rebuttals", "weak_points_of_own_case"],
    "additionalProperties": False,
}
_STRINGS = {"type": "array", "items": {"type": "string"}}
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "strongest_bull_points": _STRINGS,
        "strongest_bear_points": _STRINGS,
        "key_disagreements": _STRINGS,
        "what_would_change_the_picture": _STRINGS,
        "data_caveats": _STRINGS,
        "claims_beyond_the_data": _STRINGS,
    },
    "required": ["summary", "strongest_bull_points", "strongest_bear_points", "key_disagreements",
                 "what_would_change_the_picture", "data_caveats", "claims_beyond_the_data"],
    "additionalProperties": False,
}

RULES = """You take part in a structured research debate about one US-listed stock, for a private
investor's personal research notes. Rules, all of them binding:

1. The BRIEF below is the only data you may use. Do not use outside knowledge of the company,
   its products, news, or prices — not even to add colour. If something is not in the brief,
   say it is not in the brief.
2. Every argument and rebuttal cites the fact keys (e.g. "price.change_20d_pct") it rests on,
   in its `evidence` list. Only keys that appear in the brief count; an argument without a
   valid key is discarded.
3. No recommendation of any kind: no buy / sell / hold, no position sizing, no price target.
4. `scenarios.*` facts come from a model that sampled possible price paths. They are a
   SCENARIO RANGE, never a prediction or a forecast of the price. Say "scenario range".
5. If `company.next_earnings_confirmed` is false, the earnings date is an estimate: say so,
   never state it as a fact.
6. Backtests in the brief are small samples; do not treat their numbers as proof.
7. Write every free-text field in Hebrew. Keep tickers, numbers, units and fact keys as they are.
   Be concrete and brief: short claims, each tied to its numbers."""

ROLE = {
    "bull": "You are the BULL analyst: make the strongest honest case that the stock's outlook is "
            "favourable, using only the brief. Name the weak points of your own case too.",
    "bear": "You are the BEAR analyst: make the strongest honest case that the stock's outlook is "
            "unfavourable, using only the brief. Name the weak points of your own case too.",
}


class LLM(Protocol):
    name: str

    def complete(self, *, system: list[dict], user: str, schema: dict) -> tuple[dict, dict]:
        """(parsed JSON, usage dict with input_tokens, output_tokens, served_by)."""


@dataclass
class Usage:
    calls: list[dict] = field(default_factory=list)

    def add(self, role: str, usage: dict) -> None:
        self.calls.append({"role": role, **usage})

    def tokens(self) -> tuple[int, int]:
        return (sum(c.get("input_tokens", 0) + c.get("cache_read_input_tokens", 0)
                    + c.get("cache_creation_input_tokens", 0) for c in self.calls),
                sum(c.get("output_tokens", 0) for c in self.calls))

    def api_equivalent(self) -> float | None:
        values = [c["api_equivalent_cost_usd"] for c in self.calls
                  if isinstance(c.get("api_equivalent_cost_usd"), (int, float))]
        return round(sum(values), 4) if values else None

    def cost_usd(self, model: str) -> float | None:
        if model not in PRICES:
            return None
        p_in, p_out = PRICES[model]
        total = 0.0
        for c in self.calls:
            total += (c.get("input_tokens", 0) * p_in + c.get("output_tokens", 0) * p_out
                      + c.get("cache_creation_input_tokens", 0) * p_in * 1.25
                      + c.get("cache_read_input_tokens", 0) * p_in * 0.1) / 1e6
        return round(total, 4)


# -------------------------------------------------------------------- models
class AnthropicLLM:
    def __init__(self, *, api_key: str, model: str, effort: str, max_tokens: int, fallbacks: bool):
        try:
            import anthropic
        except ImportError as exc:
            raise ProviderError(f"the anthropic package is missing ({exc}). "
                                "Run: pip install -r requirements.txt") from exc
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key)
        self.name = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.fallbacks = fallbacks

    def complete(self, *, system: list[dict], user: str, schema: dict) -> tuple[dict, dict]:
        kwargs = dict(model=self.name, max_tokens=self.max_tokens, system=system,
                      messages=[{"role": "user", "content": user}],
                      thinking={"type": "adaptive"},
                      output_config={"effort": self.effort,
                                     "format": {"type": "json_schema", "schema": schema}})
        if self.fallbacks:
            kwargs.update(betas=[FALLBACK_BETA], fallbacks="default")
        a = self._anthropic
        try:
            response = self._client.beta.messages.create(**kwargs)
        except a.AuthenticationError as exc:
            raise ConfigError("Anthropic rejected ANTHROPIC_API_KEY (401). Fix the key in .env") from exc
        except a.PermissionDeniedError as exc:
            raise ProviderError(f"Anthropic: permission denied ({exc.message})") from exc
        except a.NotFoundError as exc:
            raise ConfigError(f"Anthropic: model '{self.name}' not found ({exc.message})") from exc
        except a.BadRequestError as exc:
            raise ProviderError(f"Anthropic rejected the request: {exc.message}") from exc
        except a.RateLimitError as exc:
            raise ProviderError("Anthropic rate limit (429) after the SDK's retries; try later") from exc
        except a.APIStatusError as exc:
            raise ProviderError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except a.APIConnectionError as exc:
            raise ProviderError(f"cannot reach the Anthropic API: {exc}") from exc

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise ProviderError(f"the model declined this request "
                                f"(category {getattr(details, 'category', None)}); nothing was used")
        if response.stop_reason == "max_tokens":
            raise ProviderError(f"the answer hit max_tokens={self.max_tokens} and is cut off; "
                                "raise debate.max_tokens")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise ProviderError(f"no text block in the answer (stop_reason {response.stop_reason})")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"the answer is not valid JSON: {exc}") from exc
        u = response.usage
        usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                 "cache_read_input_tokens": u.cache_read_input_tokens or 0,
                 "cache_creation_input_tokens": u.cache_creation_input_tokens or 0,
                 "served_by": response.model, "request_id": response._request_id}
        return parsed, usage


class ClaudeCodeLLM:
    """Claude through the local Claude Code CLI (`claude -p`), on the user's claude.ai
    subscription instead of per-call API billing (user decision, 2026-09-23).

    Read from the installed CLI 2.1.71 (its --help and the result schema in cli.js):
      claude -p --output-format json --json-schema <schema> --tools "" --model <m>
             --effort low|medium|high --system-prompt <text> --no-session-persistence
      stdout: one JSON {type: "result", subtype: "success" | "error_during_execution" |
        "error_max_turns" | "error_max_budget_usd" | "error_max_structured_output_retries",
        is_error, result, structured_output, usage, modelUsage, total_cost_usd, ...}
    Billing trap: with ANTHROPIC_API_KEY in the environment the CLI uses the key
    (`claude auth status` shows apiKeySource) and bills the API — so the child gets an
    environment without it. It also refuses to start inside a Claude Code session
    (CLAUDECODE=1); that is reported, never bypassed. The npm shim is a .cmd whose
    cmd.exe quoting would mangle JSON arguments, so cli.js is run with node directly.
    It runs in an empty temporary folder so no project CLAUDE.md is loaded.
    """

    CREDENTIAL_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    CLI_EFFORT = ("low", "medium", "high")

    def __init__(self, *, model: str, effort: str, timeout_s: int = 900, command: list[str] | None = None,
                 run=None):
        import os
        import subprocess

        if os.environ.get("CLAUDECODE") == "1":
            raise ConfigError("the debate uses Claude Code, which will not start inside another Claude "
                              "Code session. Run this command in a normal terminal (VS Code: "
                              "Terminal > New Terminal).")
        if effort not in self.CLI_EFFORT:
            raise ConfigError(f"debate.effort '{effort}' is not available through Claude Code "
                              f"(it offers {', '.join(self.CLI_EFFORT)})")
        self.command = command or self._find_cli()
        self.name = f"claude-code:{model}"
        self.model = model
        self.effort = effort
        self.timeout_s = timeout_s
        self._run = run or subprocess.run

    @staticmethod
    def _find_cli() -> list[str]:
        import shutil
        from pathlib import Path

        found = shutil.which("claude")
        if not found:
            raise ConfigError("Claude Code (the `claude` command) is not installed or not on PATH")
        cli_js = Path(found).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
        if Path(found).suffix.lower() in (".cmd", ".ps1", ".bat") or cli_js.exists():
            node = shutil.which("node")
            if not (node and cli_js.exists()):
                raise ConfigError(f"found {found} but not node + {cli_js}; cannot run Claude Code safely")
            return [node, str(cli_js)]
        return [found]

    def complete(self, *, system: list[dict], user: str, schema: dict) -> tuple[dict, dict]:
        import os
        import subprocess
        import tempfile

        system_text = "\n\n".join(block["text"] for block in system)
        args = [*self.command, "-p", "--output-format", "json", "--json-schema", json.dumps(schema),
                "--tools", "", "--no-session-persistence", "--model", self.model,
                "--effort", self.effort, "--system-prompt", system_text]
        env = {k: v for k, v in os.environ.items() if k not in self.CREDENTIAL_VARS}
        with tempfile.TemporaryDirectory(prefix="mrp-debate-") as cwd:
            try:
                done = self._run(args, input=user.encode("utf-8"), capture_output=True, cwd=cwd,
                                 env=env, timeout=self.timeout_s)
            except subprocess.TimeoutExpired as exc:
                raise ProviderError(f"Claude Code did not answer within {self.timeout_s}s") from exc
        out = done.stdout.decode("utf-8", errors="replace").strip()
        err = done.stderr.decode("utf-8", errors="replace").strip()
        try:
            result = json.loads(out)
        except json.JSONDecodeError:
            raise ProviderError(f"Claude Code (exit {done.returncode}) did not return JSON: "
                                f"{(err or out)[:500]}") from None
        if not isinstance(result, dict) or result.get("type") != "result":
            raise ProviderError(f"Claude Code returned an unexpected message: {str(result)[:300]}")
        if result.get("is_error") or result.get("subtype") != "success":
            detail = result.get("errors") or result.get("result") or err
            raise ProviderError(f"Claude Code failed ({result.get('subtype')}): {str(detail)[:500]}")
        parsed = result.get("structured_output")
        if not isinstance(parsed, dict):
            raise ProviderError("Claude Code answered without structured output "
                                f"(keys: {sorted(result)})")
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        return parsed, {
            "input_tokens": int(usage.get("input_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0) or 0),
            "cache_read_input_tokens": int(usage.get("cache_read_input_tokens", 0) or 0),
            "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens", 0) or 0),
            "served_by": ",".join(sorted(result.get("modelUsage") or {})) or self.model,
            # What the same tokens would cost on the API; not billed on a subscription.
            "api_equivalent_cost_usd": result.get("total_cost_usd"),
        }


class SyntheticLLM:
    """Offline stand-in for --selftest and tests: canned Hebrew text citing real keys.
    Never selected unless config asks for it."""

    name = "synthetic"

    def __init__(self, keys: list[str] | None = None):
        self.keys = keys
        self.prompts: list[str] = []

    def complete(self, *, system: list[dict], user: str, schema: dict) -> tuple[dict, dict]:
        self.prompts.append(user)
        keys = self.keys or ["price.last_close"]
        usage = {"input_tokens": 1000, "output_tokens": 200, "served_by": "synthetic"}
        if schema is SUMMARY_SCHEMA:
            return ({"summary": "סיכום לדוגמה.", "strongest_bull_points": ["נקודה שורית"],
                     "strongest_bear_points": ["נקודה דובית"], "key_disagreements": ["מחלוקת"],
                     "what_would_change_the_picture": ["נתון שחסר"], "data_caveats": ["הסתייגות"],
                     "claims_beyond_the_data": []}, usage)
        return ({"thesis": "תזה לדוגמה.",
                 "arguments": [{"claim": "טענה לדוגמה", "evidence": keys[:1]}],
                 "rebuttals": [{"claim": "תשובה לדוגמה", "evidence": keys[:1]}]
                 if "OPPONENT" in user else [],
                 "weak_points_of_own_case": ["חולשה לדוגמה"]}, usage)


# ------------------------------------------------------------------ checks
def check_shape(obj: dict, schema: dict, role: str) -> dict:
    """The API enforces the schema; check the fields we read anyway."""
    missing = [k for k in schema["required"] if k not in obj]
    if missing:
        raise ProviderError(f"{role}: answer lacks {missing}; has {sorted(obj)}")
    return obj


def ground(statement: dict, fact_keys: set[str]) -> tuple[dict, list[dict]]:
    """Keep arguments whose evidence is non-empty and entirely brief keys."""
    kept, dropped = dict(statement), []
    for part in ("arguments", "rebuttals"):
        good = []
        for arg in statement.get(part, []):
            evidence = [e for e in arg.get("evidence", []) if isinstance(e, str)]
            unknown = [e for e in evidence if e not in fact_keys]
            if not evidence or unknown or not str(arg.get("claim", "")).strip():
                dropped.append({"part": part, "claim": arg.get("claim"), "evidence": evidence,
                                "unknown_keys": unknown,
                                "reason": "no citation" if not evidence else "unknown fact key"})
            else:
                good.append({"claim": arg["claim"], "evidence": evidence})
        kept[part] = good
    return kept, dropped


# -------------------------------------------------------------------- flow
def system_blocks(brief: dict) -> list[dict]:
    """Rules + brief: identical for every call in a run, so it is cached."""
    return [{"type": "text", "text": RULES},
            {"type": "text", "text": "BRIEF (JSON):\n" + json.dumps(brief, ensure_ascii=False, indent=1),
             "cache_control": {"type": "ephemeral"}}]


def run_debate(brief: dict, llm: LLM, *, rounds: int, max_cost_usd: float) -> dict:
    fact_keys = set(brief["facts"])
    if not fact_keys:
        raise ProviderError("the brief has no facts; nothing to debate")
    system = system_blocks(brief)
    usage = Usage()

    def call(role: str, user: str, schema: dict) -> dict:
        spent = usage.cost_usd(llm.name)
        if spent is not None and spent >= max_cost_usd:
            raise ProviderError(f"debate stopped: estimated spend ${spent:.2f} reached "
                                f"debate.max_cost_usd ${max_cost_usd:.2f}")
        obj, u = llm.complete(system=system, user=user, schema=schema)
        usage.add(role, u)
        log.info("debate: %s answered (%s in / %s out tokens, served by %s)", role,
                 u.get("input_tokens"), u.get("output_tokens"), u.get("served_by"))
        return check_shape(obj, schema, role)

    statements: dict[str, list[dict]] = {"bull": [], "bear": []}
    dropped: list[dict] = []

    def keep(side: str, rnd: int, obj: dict) -> None:
        grounded, lost = ground(obj, fact_keys)
        statements[side].append({"round": rnd, **grounded})
        dropped.extend({"side": side, "round": rnd, **d} for d in lost)

    for side in ("bull", "bear"):
        keep(side, 1, call(f"{side}-1", f"{ROLE[side]}\n\nRound 1: opening statement. Leave "
                                         "`rebuttals` empty.", STATEMENT_SCHEMA))
    for rnd in range(2, rounds + 1):
        previous = {s: statements[s][-1] for s in statements}
        for side, other in (("bull", "bear"), ("bear", "bull")):
            keep(side, rnd, call(
                f"{side}-{rnd}",
                f"{ROLE[side]}\n\nRound {rnd}: rebut the OPPONENT's last statement below (in "
                "`rebuttals`), and restate or sharpen your own arguments.\n\nOPPONENT:\n"
                + json.dumps(previous[other], ensure_ascii=False), STATEMENT_SCHEMA))

    summary = call("moderator",
                   "You are the MODERATOR. You do not take a side and you do not decide. Summarise the "
                   "debate below for the investor: the strongest points on each side, where they "
                   "really disagree, which missing data would change the picture, the data caveats, "
                   "and any claim either side made that goes beyond the brief (list it in "
                   "`claims_beyond_the_data`).\n\nDEBATE:\n"
                   + json.dumps(statements, ensure_ascii=False), SUMMARY_SCHEMA)

    arguments = sum(len(s["arguments"]) + len(s["rebuttals"]) for side in statements.values() for s in side)
    tokens_in, tokens_out = usage.tokens()
    return {
        "model": llm.name,
        "rounds": rounds,
        "language": "he",
        "statements": statements,
        "moderator": summary,
        "dropped_arguments": dropped,
        "grounded_arguments": arguments,
        "usage": {"calls": usage.calls, "input_tokens": tokens_in, "output_tokens": tokens_out,
                  "estimated_cost_usd": usage.cost_usd(llm.name),
                  "api_equivalent_cost_usd": usage.api_equivalent(),
                  "cost_note": ("Claude Code on a subscription: not billed per call; the API-"
                                "equivalent figure is what the same tokens would cost on the API"
                                if isinstance(llm, ClaudeCodeLLM)
                                else "estimate at the requested model's list price")},
        "caveats": ["Two argued cases and a neutral summary, written by a language model from the "
                    "brief only. Not investment advice and not a recommendation."],
    }
