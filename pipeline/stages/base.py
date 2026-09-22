"""Stage contract: each stage reads prior artifacts, writes its own, returns a summary."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..cache import RunContext

log = logging.getLogger(__name__)


@dataclass
class StageResult:
    stage: str
    status: str                      # ok | skipped | not_implemented
    summary: str = ""
    artifacts: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)


class Stage(ABC):
    name: str = "base"
    requires_validation: bool = True   # every stage after validate gates on it

    @abstractmethod
    def run(self, ctx: RunContext) -> StageResult: ...

    def _gate(self, ctx: RunContext) -> None:
        """Called by the runner before run(); enforces the HALT rule."""
        if self.requires_validation:
            from ..gate import require_validation_pass
            require_validation_pass(ctx)


class NotImplementedStage(Stage):
    """Registered placeholder so `--stages` accepts the name and partial runs work."""

    def __init__(self, name: str, phase: str):
        self.name = name
        self.phase = phase

    def run(self, ctx: RunContext) -> StageResult:
        return StageResult(
            stage=self.name,
            status="not_implemented",
            summary=f"'{self.name}' arrives in {self.phase}",
        )

    def _gate(self, ctx: RunContext) -> None:  # placeholders never gate
        return
