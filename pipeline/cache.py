"""Per-run artifact store.

Layout:  cache/<TICKER>/<RUN_DATE>/<artifact>.parquet  (+ manifest.json)

Stages never pass DataFrames to each other in memory — they read the previous
stage's Parquet. That is what makes `--stages forecast` rerunnable on its own.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .contracts import Contract

log = logging.getLogger(__name__)

MANIFEST = "manifest.json"


@dataclass
class RunContext:
    ticker: str
    run_date: str          # YYYY-MM-DD
    run_dir: Path
    settings: object       # pipeline.config.Settings

    @property
    def manifest_path(self) -> Path:
        return self.run_dir / MANIFEST

    # ---------------------------------------------------------------- artifacts
    def path(self, artifact: str, suffix: str = ".parquet") -> Path:
        return self.run_dir / f"{artifact}{suffix}"

    def exists(self, artifact: str, suffix: str = ".parquet") -> bool:
        return self.path(artifact, suffix).exists()

    def write_parquet(self, artifact: str, df: pd.DataFrame, contract: Contract | None = None) -> Path:
        if contract is not None:
            contract.validate(df)
        target = self.path(artifact)
        target.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(target, index=False)
        log.info("wrote %s (%d rows, %d cols)", target.name, len(df), len(df.columns))
        self.record(artifact, rows=len(df))
        return target

    def read_parquet(self, artifact: str, contract: Contract | None = None) -> pd.DataFrame:
        target = self.path(artifact)
        if not target.exists():
            raise FileNotFoundError(
                f"{target} not found — run the stage that produces '{artifact}' first."
            )
        df = pd.read_parquet(target)
        if contract is not None:
            contract.validate(df)
        return df

    def write_json(self, artifact: str, payload: dict) -> Path:
        target = self.path(artifact, ".json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("wrote %s", target.name)
        return target

    def read_json(self, artifact: str) -> dict:
        target = self.path(artifact, ".json")
        if not target.exists():
            raise FileNotFoundError(f"{target} not found")
        return json.loads(target.read_text(encoding="utf-8"))

    # ---------------------------------------------------------------- manifest
    def load_manifest(self) -> dict:
        if not self.manifest_path.exists():
            return {"ticker": self.ticker, "run_date": self.run_date, "artifacts": {}, "stages": {}}
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def record(self, artifact: str, **meta) -> None:
        manifest = self.load_manifest()
        manifest.setdefault("artifacts", {})[artifact] = {
            "written_at": datetime.now(timezone.utc).isoformat(),
            **meta,
        }
        self._save(manifest)

    def record_stage(self, stage: str, status: str, **meta) -> None:
        manifest = self.load_manifest()
        manifest.setdefault("stages", {})[stage] = {
            "status": status,
            "at": datetime.now(timezone.utc).isoformat(),
            **meta,
        }
        self._save(manifest)

    def _save(self, manifest: dict) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def make_run_context(settings, ticker: str, run_date: str | None = None) -> RunContext:
    ticker = ticker.strip().upper()
    if not ticker:
        raise ValueError("ticker must not be empty")
    date = run_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    run_dir = Path(settings.cache_dir) / ticker / date
    run_dir.mkdir(parents=True, exist_ok=True)
    return RunContext(ticker=ticker, run_date=date, run_dir=run_dir, settings=settings)
