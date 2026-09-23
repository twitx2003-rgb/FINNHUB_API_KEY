"""Phase 5: the debate brief, grounding, the cost cap, the Anthropic adapter and the stage.

Offline: the adapter runs against a fake `anthropic` module shaped like the SDK
calls it makes; the stage uses the synthetic stand-in. No key, no money.
"""
from __future__ import annotations

import dataclasses
import json
import shutil
import sys
import types
from pathlib import Path

import pandas as pd
import pytest

from pipeline.brief import Brief, add_options, add_reference, build_brief
from pipeline.config import DebateSettings, load_settings
from pipeline.debate import (
    FALLBACK_BETA,
    STATEMENT_SCHEMA,
    SUMMARY_SCHEMA,
    AnthropicLLM,
    ClaudeCodeLLM,
    SyntheticLLM,
    ground,
    run_debate,
)
from pipeline.errors import ConfigError, PipelineHalt, ProviderError
from pipeline.cache import make_run_context
from pipeline.stages import build_registry
from pipeline.stages.data import DataStage
from pipeline.stages.debate import DebateStage
from pipeline.stages.forecast import ForecastStage
from pipeline.stages.validate import ValidateStage

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    s = load_settings(tmp_path / "config.yaml", root=tmp_path)
    s = dataclasses.replace(
        s,
        data=dataclasses.replace(s.data, provider="synthetic", fallback_provider=None, macro_fallback=None),
        validate=dataclasses.replace(s.validate, provider="synthetic"),
        forecast=dataclasses.replace(s.forecast, timesfm_provider="synthetic", kronos_provider="synthetic"),
        debate=dataclasses.replace(s.debate, provider="synthetic"))
    c = make_run_context(s, "TEST", "1970-01-01")
    DataStage().run(c)
    ValidateStage().run(c)
    ForecastStage().run(c)
    return c


# ------------------------------------------------------------------- brief
def test_brief_holds_only_sourced_facts_and_lists_what_is_missing(ctx):
    brief = build_brief(ctx, ctx.read_json("validation")).to_dict()
    facts = brief["facts"]
    assert {"price.last_close", "price.change_20d_pct", "volatility.realized_20d_pct",
            "scenarios.close_change_range_pct"} <= set(facts)
    assert all({"value", "unit", "as_of", "source"} <= set(f) for f in facts.values())
    assert "extract_insider" in brief["missing"] and "extract_holders" in brief["missing"]
    assert "NOT a prediction" in facts["scenarios.close_change_range_pct"]["note"]
    last = ctx.read_parquet("data_ohlcv")["close"].iloc[-1]
    assert facts["price.last_close"]["value"] == pytest.approx(last)


def test_unconfirmed_earnings_are_marked_as_estimates():
    brief = Brief("T", "2026-09-22")
    validation = {"status": "pass", "checks": [{"name": "next_earnings", "status": "warn", "confirmed": False}]}
    add_reference(brief, {"next_earnings": {"dates": ["2026-11-17"], "source": "yahoo"}}, validation)
    assert brief.facts["company.next_earnings_confirmed"]["value"] is False
    assert "NOT CONFIRMED" in brief.facts["company.next_earnings_date"]["note"]


def test_option_iv_in_percent_is_refused_not_rescaled():
    options = pd.DataFrame({"dte": [30], "strike": [100.0], "underlying_price": [100.0],
                            "implied_volatility": [40.0], "type": ["call"], "volume": [5.0],
                            "updated_at": ["2026-09-22"]})
    with pytest.raises(ProviderError, match="looks like percent"):
        add_options(Brief("T", "d"), options)


def test_a_fact_cannot_be_added_twice():
    brief = Brief("T", "d")
    brief.add("x", 1.0, unit="u", as_of=None, source="s")
    with pytest.raises(ProviderError, match="added twice"):
        brief.add("x", 2.0, unit="u", as_of=None, source="s")


# --------------------------------------------------------------- grounding
def test_arguments_without_valid_citations_are_dropped():
    statement = {"thesis": "t", "weak_points_of_own_case": [], "arguments": [
        {"claim": "ok", "evidence": ["price.last_close"]},
        {"claim": "no source", "evidence": []},
        {"claim": "made up", "evidence": ["price.last_close", "news.ceo_quote"]},
    ], "rebuttals": [{"claim": "r", "evidence": ["x.y"]}]}
    kept, dropped = ground(statement, {"price.last_close"})
    assert [a["claim"] for a in kept["arguments"]] == ["ok"] and kept["rebuttals"] == []
    assert [(d["claim"], d["reason"]) for d in dropped] == [
        ("no source", "no citation"), ("made up", "unknown fact key"), ("r", "unknown fact key")]
    assert dropped[1]["unknown_keys"] == ["news.ceo_quote"]


# -------------------------------------------------------------------- flow
def small_brief():
    return {"ticker": "T", "run_date": "d", "missing": [], "caveats": [],
            "facts": {"price.last_close": {"value": 1.0, "unit": "USD", "as_of": "d", "source": "s"}}}


def test_debate_runs_openings_rebuttals_and_a_moderator():
    llm = SyntheticLLM(keys=["price.last_close"])
    out = run_debate(small_brief(), llm, rounds=2, max_cost_usd=10)
    assert len(llm.prompts) == 5                               # 2 x rounds + moderator
    assert sum("OPPONENT" in p for p in llm.prompts) == 2
    assert [s["round"] for s in out["statements"]["bull"]] == [1, 2]
    assert out["statements"]["bear"][1]["rebuttals"]          # round 2 answered the other side
    assert out["language"] == "he" and out["moderator"]["summary"]
    assert out["usage"]["input_tokens"] == 5000 and out["usage"]["estimated_cost_usd"] is None


def test_every_call_sees_the_same_brief_and_rules():
    seen = []

    class Recorder(SyntheticLLM):
        def complete(self, *, system, user, schema):
            seen.append(json.dumps(system, ensure_ascii=False))
            return super().complete(system=system, user=user, schema=schema)

    run_debate(small_brief(), Recorder(keys=["price.last_close"]), rounds=1, max_cost_usd=10)
    assert len(set(seen)) == 1 and "price.last_close" in seen[0] and "No recommendation" in seen[0]


def test_cost_cap_stops_before_the_next_call():
    class Expensive(SyntheticLLM):
        name = "claude-opus-5"

        def complete(self, **kw):
            obj, _ = super().complete(**kw)
            return obj, {"input_tokens": 100_000, "output_tokens": 40_000}   # $1.50 per call

    with pytest.raises(ProviderError, match="max_cost_usd"):
        run_debate(small_brief(), Expensive(keys=["price.last_close"]), rounds=2, max_cost_usd=2.0)


def test_empty_brief_is_refused():
    with pytest.raises(ProviderError, match="no facts"):
        run_debate({**small_brief(), "facts": {}}, SyntheticLLM(), rounds=1, max_cost_usd=1)


# ---------------------------------------------------------------- adapter
def fake_anthropic(monkeypatch, *, stop_reason="end_turn", text=None):
    calls = []

    class APIError(Exception):
        def __init__(self, message="", status_code=500):
            super().__init__(message)
            self.message, self.status_code = message, status_code

    names = ["AuthenticationError", "PermissionDeniedError", "NotFoundError", "BadRequestError",
             "RateLimitError", "APIStatusError", "APIConnectionError"]
    module = types.ModuleType("anthropic")
    for n in names:
        setattr(module, n, type(n, (APIError,), {}))

    body = text if text is not None else json.dumps(
        {"thesis": "ת", "arguments": [], "rebuttals": [], "weak_points_of_own_case": []})

    class Messages:
        def create(self, **kwargs):
            calls.append(kwargs)
            return types.SimpleNamespace(
                stop_reason=stop_reason, stop_details=types.SimpleNamespace(category="cyber"),
                content=[types.SimpleNamespace(type="thinking", thinking=""),
                         types.SimpleNamespace(type="text", text=body)],
                usage=types.SimpleNamespace(input_tokens=10, output_tokens=5,
                                            cache_read_input_tokens=None, cache_creation_input_tokens=7),
                model="claude-opus-5", _request_id="req_1")

    class Client:
        def __init__(self, api_key):
            calls.append({"api_key": api_key})
            self.beta = types.SimpleNamespace(messages=Messages())

    module.Anthropic = Client
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return calls


def make_llm(**kw):
    return AnthropicLLM(api_key="sk-ant-test", model="claude-opus-5", effort="high",
                        max_tokens=16000, fallbacks=kw.get("fallbacks", True))


def test_adapter_sends_schema_effort_thinking_and_fallbacks(monkeypatch):
    calls = fake_anthropic(monkeypatch)
    obj, usage = make_llm().complete(system=[{"type": "text", "text": "s"}], user="u",
                                     schema=STATEMENT_SCHEMA)
    req = calls[1]
    assert req["model"] == "claude-opus-5" and req["thinking"] == {"type": "adaptive"}
    assert req["output_config"] == {"effort": "high",
                                    "format": {"type": "json_schema", "schema": STATEMENT_SCHEMA}}
    assert req["betas"] == [FALLBACK_BETA] and req["fallbacks"] == "default"
    assert obj["thesis"] == "ת"
    assert usage == {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0,
                     "cache_creation_input_tokens": 7, "served_by": "claude-opus-5",
                     "request_id": "req_1"}


def test_adapter_without_fallbacks_sends_no_beta(monkeypatch):
    calls = fake_anthropic(monkeypatch)
    make_llm(fallbacks=False).complete(system=[], user="u", schema=SUMMARY_SCHEMA)
    assert "betas" not in calls[1] and "fallbacks" not in calls[1]


@pytest.mark.parametrize("stop,match", [("refusal", "declined"), ("max_tokens", "max_tokens")])
def test_adapter_checks_the_stop_reason_before_reading(monkeypatch, stop, match):
    fake_anthropic(monkeypatch, stop_reason=stop)
    with pytest.raises(ProviderError, match=match):
        make_llm().complete(system=[], user="u", schema=STATEMENT_SCHEMA)


def test_adapter_refuses_non_json(monkeypatch):
    fake_anthropic(monkeypatch, text="not json")
    with pytest.raises(ProviderError, match="not valid JSON"):
        make_llm().complete(system=[], user="u", schema=STATEMENT_SCHEMA)


# ------------------------------------------------------------------- stage
def test_debate_is_a_real_stage_behind_the_gate(tmp_path):
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    c = make_run_context(load_settings(tmp_path / "config.yaml", root=tmp_path), "T", "2026-09-22")
    stage = build_registry()["debate"]
    assert isinstance(stage, DebateStage)
    with pytest.raises(PipelineHalt, match="has not run"):
        stage._gate(c)


def test_stage_writes_the_brief_and_the_debate(ctx):
    stage = DebateStage()
    stage._gate(ctx)
    result = stage.run(ctx)
    assert result.artifacts == ["debate_brief", "debate"]
    debate = ctx.read_json("debate")
    brief = ctx.read_json("debate_brief")
    cited = {e for side in debate["statements"].values() for s in side for a in s["arguments"]
             for e in a["evidence"]}
    assert cited and cited <= set(brief["facts"])
    assert "Not investment advice" in debate["caveats"][0]


def test_missing_key_without_a_terminal_fails_before_any_call(ctx, monkeypatch):
    ctx.settings = dataclasses.replace(ctx.settings, debate=DebateSettings(provider="anthropic",
                                                                           model="claude-opus-5"))
    monkeypatch.setattr("sys.stdin", types.SimpleNamespace(isatty=lambda: False))
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY is not set"):
        DebateStage().run(ctx)


def test_a_secret_answer_is_never_repeated(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    settings = load_settings(tmp_path / "config.yaml", root=tmp_path)
    with pytest.raises(ConfigError) as err:
        settings.env_or_ask("ANTHROPIC_API_KEY", "q?", must_contain="sk-ant-", secret=True,
                            ask=lambda p: "wrong-secret-value", is_interactive=True)
    assert "wrong-secret-value" not in str(err.value)


@pytest.mark.parametrize("kwargs,match", [
    ({"rounds": 4}, "1..3"), ({"effort": "huge"}, "effort"), ({"provider": "openai"}, "provider"),
    ({"provider": "claude_code", "effort": "max"}, "low/medium/high only"),
    ({"provider": "anthropic", "model": "sonnet"}, "not an API model id"),
])
def test_config_refuses_bad_debate_settings(kwargs, match):
    with pytest.raises(ConfigError, match=match):
        DebateSettings(**kwargs)


def test_default_debate_runs_on_claude_code():
    assert DebateSettings().provider == "claude_code"
    assert load_settings().debate.provider == "claude_code"


# ------------------------------------------------------------ Claude Code
def cli_result(**over):
    body = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
            "result": "", "stop_reason": "end_turn", "total_cost_usd": 0.042,
            "usage": {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 50},
            "modelUsage": {"claude-sonnet-5": {}}, "session_id": "s",
            "structured_output": {"thesis": "ת", "arguments": [], "rebuttals": [],
                                  "weak_points_of_own_case": []}}
    body.update(over)
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


class FakeRun:
    def __init__(self, stdout=None, returncode=0, stderr=b""):
        self.stdout, self.returncode, self.stderr = stdout or cli_result(), returncode, stderr
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return types.SimpleNamespace(stdout=self.stdout, stderr=self.stderr, returncode=self.returncode)


@pytest.fixture
def outside_claude_code(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")


def test_claude_code_call_drops_the_api_key_and_sends_schema(outside_claude_code):
    run = FakeRun()
    llm = ClaudeCodeLLM(model="sonnet", effort="high", command=["node", "cli.js"], run=run)
    obj, usage = llm.complete(system=[{"type": "text", "text": "RULES"},
                                      {"type": "text", "text": "BRIEF"}],
                              user="בול: פתיחה", schema=STATEMENT_SCHEMA)
    args, kw = run.calls[0]
    assert args[:3] == ["node", "cli.js", "-p"]
    flag = lambda name: args[args.index(name) + 1]
    assert json.loads(flag("--json-schema")) == STATEMENT_SCHEMA
    assert flag("--tools") == "" and flag("--model") == "sonnet" and flag("--effort") == "high"
    assert flag("--system-prompt") == "RULES\n\nBRIEF" and "--no-session-persistence" in args
    assert "ANTHROPIC_API_KEY" not in kw["env"] and "ANTHROPIC_AUTH_TOKEN" not in kw["env"]
    assert kw["input"].decode("utf-8") == "בול: פתיחה"
    assert Path(kw["cwd"]).resolve() != ROOT.resolve() and not Path(kw["cwd"]).exists()
    assert obj["thesis"] == "ת"
    assert usage["input_tokens"] == 1200 and usage["served_by"] == "claude-sonnet-5"
    assert usage["api_equivalent_cost_usd"] == 0.042


@pytest.mark.parametrize("stdout,match", [
    (cli_result(subtype="error_max_structured_output_retries", is_error=True), "structured_output_retries"),
    (cli_result(structured_output=None), "without structured output"),
    (b"Error: not logged in", "did not return JSON"),
    (json.dumps({"type": "assistant"}).encode(), "unexpected message"),
])
def test_claude_code_failures_are_loud(outside_claude_code, stdout, match):
    llm = ClaudeCodeLLM(model="sonnet", effort="high", command=["c"], run=FakeRun(stdout=stdout))
    with pytest.raises(ProviderError, match=match):
        llm.complete(system=[], user="u", schema=STATEMENT_SCHEMA)


def test_claude_code_is_not_started_inside_a_claude_code_session(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    with pytest.raises(ConfigError, match="normal terminal"):
        ClaudeCodeLLM(model="sonnet", effort="high", command=["c"], run=FakeRun())


def test_claude_code_npm_shim_is_bypassed_for_node(outside_claude_code, tmp_path, monkeypatch):
    shim = tmp_path / "claude.cmd"
    shim.write_text("@echo off", encoding="utf-8")
    cli = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("//", encoding="utf-8")
    monkeypatch.setattr("shutil.which", lambda name: {"claude": str(shim), "node": "C:/node.exe"}[name])
    assert ClaudeCodeLLM(model="sonnet", effort="low", run=FakeRun()).command == ["C:/node.exe", str(cli)]


def test_subscription_run_reports_api_equivalent_not_a_charge(outside_claude_code):
    llm = ClaudeCodeLLM(model="sonnet", effort="high", command=["c"], run=FakeRun(
        stdout=cli_result(structured_output={"thesis": "t", "arguments": [
            {"claim": "c", "evidence": ["price.last_close"]}], "rebuttals": [],
            "weak_points_of_own_case": [], "summary": "s", "strongest_bull_points": [],
            "strongest_bear_points": [], "key_disagreements": [], "what_would_change_the_picture": [],
            "data_caveats": [], "claims_beyond_the_data": []})))
    out = run_debate(small_brief(), llm, rounds=1, max_cost_usd=0.01)   # cap is for the API only
    assert out["usage"]["estimated_cost_usd"] is None
    assert out["usage"]["api_equivalent_cost_usd"] == pytest.approx(0.126)
    assert "not billed" in out["usage"]["cost_note"]
