"""Stage 6 — debate (phase 5): bull vs bear + a moderator, in Hebrew, from the brief.

Reads only validated artifacts of this run (pipeline/brief.py), writes the brief
it used (`debate_brief.json`) next to the debate (`debate.json`) so the report
can show exactly what the model was given. Default provider: Claude Code on the
user's subscription (no per-call charge); the API provider costs money per run.
"""
from __future__ import annotations

import logging
from typing import Callable

from ..brief import build_brief
from ..cache import RunContext
from ..debate import LLM, AnthropicLLM, ClaudeCodeLLM, SyntheticLLM, run_debate
from ..gate import require_validation_pass
from .base import Stage, StageResult

log = logging.getLogger(__name__)

ARTIFACT = "debate"
BRIEF_ARTIFACT = "debate_brief"

KEY_QUESTION = ("The debate stage calls the Anthropic API (paid per run). Paste your "
                "ANTHROPIC_API_KEY (starts with sk-ant-; it is not shown while you type):")


def default_llm(ctx: RunContext) -> LLM:
    d = ctx.settings.debate
    if d.provider == "synthetic":
        return SyntheticLLM()
    if d.provider == "claude_code":
        return ClaudeCodeLLM(model=d.model, effort=d.effort)
    key =ctx.settings.env_or_ask("ANTHROPIC_API_KEY", KEY_QUESTION, must_contain="sk-ant-",
                                  secret=True)
    return AnthropicLLM(api_key=key, model=d.model, effort=d.effort, max_tokens=d.max_tokens,
                        fallbacks=d.fallbacks)


class DebateStage(Stage):
    name = "debate"

    def __init__(self, llm_factory: Callable[[RunContext], LLM] = default_llm):
        self.llm_factory = llm_factory

    def run(self, ctx: RunContext) -> StageResult:
        d = ctx.settings.debate
        if not d.enabled:
            return StageResult(stage=self.name, status="skipped", summary="debate disabled in config")

        brief = build_brief(ctx, require_validation_pass(ctx)).to_dict()
        ctx.write_json(BRIEF_ARTIFACT, brief)
        log.info("debate brief: %d facts, missing %s", len(brief["facts"]), brief["missing"] or "nothing")

        llm = self.llm_factory(ctx)
        calls = 2 * d.rounds + 1
        if d.provider == "anthropic":
            log.warning("debate: calling %s (%d rounds + moderator = %d paid calls, stop at an "
                        "estimated $%.2f)", d.model, d.rounds, calls, d.max_cost_usd)
        elif d.provider == "claude_code":
            log.info("debate: %d calls to Claude Code (%s, effort %s) on your subscription — no "
                     "per-call charge; they count toward the plan's usage limits", calls, d.model,
                     d.effort)
        result = run_debate(brief, llm, rounds=d.rounds, max_cost_usd=d.max_cost_usd)
        ctx.write_json(ARTIFACT, result)

        u = result["usage"]
        cost = u["estimated_cost_usd"]
        if cost is None and u.get("api_equivalent_cost_usd") is not None:
            log.info("debate: the same tokens would cost ~$%.2f on the API (not billed on a "
                     "subscription)", u["api_equivalent_cost_usd"])
        dropped = len(result["dropped_arguments"])
        if dropped:
            log.warning("debate: %d argument(s) dropped for citing no fact or an unknown fact "
                        "(listed in debate.json)", dropped)
        return StageResult(
            stage=self.name, status="ok",
            summary=(f"{llm.name}: {result['grounded_arguments']} grounded argument(s), {dropped} "
                     f"dropped; {u['input_tokens']} in / {u['output_tokens']} out tokens"
                     + (f", ~${cost:.2f}" if cost is not None else "")),
            artifacts=[BRIEF_ARTIFACT, ARTIFACT],
            details={"usage": {k: v for k, v in u.items() if k != "calls"}},
        )
