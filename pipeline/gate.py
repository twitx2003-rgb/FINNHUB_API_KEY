"""The validation gate.

Structural enforcement of the rule "a validation failure stops everything
downstream". Stages call require_validation_pass() as their first statement, so
the check cannot be forgotten, and `--stages report` on a failed run refuses to
start rather than quietly rendering unvalidated numbers.
"""
from __future__ import annotations

import logging

from .cache import RunContext
from .errors import PipelineHalt

log = logging.getLogger(__name__)

VALIDATION_ARTIFACT = "validation"


def require_validation_pass(ctx: RunContext) -> dict:
    if not ctx.exists(VALIDATION_ARTIFACT, ".json"):
        raise PipelineHalt(
            "Validation has not run for "
            f"{ctx.ticker} {ctx.run_date}. Run: python run.py --ticker "
            f"{ctx.ticker} --stages data,validate"
        )

    report = ctx.read_json(VALIDATION_ARTIFACT)
    status = report.get("status")
    if status != "pass":
        failed = [c.get("name") for c in report.get("checks", []) if c.get("status") == "fail"]
        raise PipelineHalt(
            f"Validation status is '{status}' for {ctx.ticker} {ctx.run_date}"
            + (f" (failed: {', '.join(filter(None, failed))})" if failed else "")
            + ". Downstream stages are blocked."
        )
    return report
