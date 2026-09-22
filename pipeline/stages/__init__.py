"""Stage registry and execution order.

Later-phase stages are registered as placeholders so `--stages` accepts their
names from day one and the dependency order is visible in one place.
"""
from __future__ import annotations

from .base import NotImplementedStage, Stage
from .data import DataStage
from .validate import ValidateStage

# Execution order. `--stages all` runs them in this sequence.
STAGE_ORDER: tuple[str, ...] = (
    "data", "validate", "extract", "docs", "forecast", "debate", "report",
)

_PLACEHOLDERS = {
    "forecast": "phase 3 (TimesFM) and phase 5 (Kronos)",
    "extract": "phase 4",
    "docs": "phase 4",
    "debate": "phase 5",
    "report": "phase 5",
}


def build_registry() -> dict[str, Stage]:
    registry: dict[str, Stage] = {"data": DataStage(), "validate": ValidateStage()}
    for name, phase in _PLACEHOLDERS.items():
        registry[name] = NotImplementedStage(name, phase)
    return registry


def resolve_stages(requested: str) -> list[str]:
    """'all' -> every stage in order; 'data,validate' -> those, in canonical order."""
    text = (requested or "all").strip().lower()
    if text == "all":
        return list(STAGE_ORDER)

    names = [part.strip() for part in text.split(",") if part.strip()]
    unknown = [n for n in names if n not in STAGE_ORDER]
    if unknown:
        raise ValueError(
            f"Unknown stage(s): {unknown}. Valid: {', '.join(STAGE_ORDER)} (or 'all')"
        )
    return [s for s in STAGE_ORDER if s in set(names)]
