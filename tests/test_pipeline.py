"""Wiring: stage resolution, the HALT gate, and a full offline data-stage run."""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from pipeline.cache import make_run_context
from pipeline.config import load_settings
from pipeline.errors import PipelineHalt
from pipeline.gate import require_validation_pass
from pipeline.stages import resolve_stages, build_registry
from pipeline.providers.synthetic import SyntheticProvider
from pipeline.stages.data import DataStage, print_close_preview


# ------------------------------------------------------------------ stages
def test_resolve_all_returns_canonical_order():
    assert resolve_stages("all") == [
        "data", "validate", "extract", "docs", "forecast", "debate", "report"
    ]


def test_resolve_partial_keeps_canonical_order_not_input_order():
    assert resolve_stages("validate,data") == ["data", "validate"]


def test_resolve_rejects_unknown_stage():
    with pytest.raises(ValueError, match="Unknown stage"):
        resolve_stages("data,nonsense")


def test_every_stage_name_is_registered():
    registry = build_registry()
    assert set(registry) == set(resolve_stages("all"))


# -------------------------------------------------------------------- gate
@pytest.fixture
def ctx(tmp_path, monkeypatch):
    import shutil
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    shutil.copy(root / "config.yaml", tmp_path / "config.yaml")
    settings = load_settings(tmp_path / "config.yaml", root=tmp_path)
    return make_run_context(settings, "NVDA", "2026-09-22")


def test_gate_halts_when_validation_missing(ctx):
    with pytest.raises(PipelineHalt, match="has not run"):
        require_validation_pass(ctx)


def test_gate_halts_when_validation_failed(ctx):
    ctx.write_json("validation", {
        "status": "fail",
        "checks": [{"name": "last_close", "status": "fail"}],
    })
    with pytest.raises(PipelineHalt, match="last_close"):
        require_validation_pass(ctx)


def test_gate_passes_when_validation_ok(ctx):
    ctx.write_json("validation", {"status": "pass", "checks": []})
    assert require_validation_pass(ctx)["status"] == "pass"


def test_downstream_stage_gate_blocks_without_validation(ctx):
    """`--stages report` on an unvalidated run must refuse to start."""
    report_stage = build_registry()["report"]
    report_stage.requires_validation = True   # placeholders opt out; force the real rule
    from pipeline.stages.base import Stage
    with pytest.raises(PipelineHalt):
        Stage._gate(report_stage, ctx)


# ------------------------------------------------- full offline data stage
# The synthetic provider lives in the package so --selftest and the tests share it.
FakeProvider = SyntheticProvider


def test_data_stage_writes_all_three_artifacts(ctx, monkeypatch, capsys):
    monkeypatch.setattr("pipeline.stages.data.get_provider",
                        lambda name, settings=None: FakeProvider())

    result = DataStage().run(ctx)

    assert result.status == "ok"
    assert set(result.artifacts) == {"data_ohlcv", "data_options", "data_macro"}
    for artifact in result.artifacts:
        assert ctx.exists(artifact), f"{artifact}.parquet was not written"

    # Stored chronologically, regardless of the desc request used to fetch it.
    ohlcv = ctx.read_parquet("data_ohlcv")
    assert ohlcv["timestamp"].is_monotonic_increasing

    manifest = ctx.load_manifest()
    assert manifest["artifacts"]["data_ohlcv"]["rows"] == 30

    print_close_preview(ctx)
    assert "LATEST CLOSE" in capsys.readouterr().out


def test_data_stage_rejects_stale_provider_data(ctx, monkeypatch):
    class StaleProvider(FakeProvider):
        def daily_ohlcv(self, symbol, lookback_days):
            df = super().daily_ohlcv(symbol, lookback_days)
            df["timestamp"] = pd.date_range("2003-01-02", periods=len(df), freq="D", tz="UTC")
            return df

    monkeypatch.setattr("pipeline.stages.data.get_provider",
                        lambda name, settings=None: StaleProvider())
    from pipeline.errors import StaleDataError
    with pytest.raises(StaleDataError):
        DataStage().run(ctx)


# -------------------------------------------------------------- self-test
def test_synthetic_provider_is_registered():
    from pipeline.providers import get_provider
    assert get_provider("synthetic").name == "synthetic"


def test_selftest_passes_without_network_or_key(capsys):
    """`--selftest` must work with no LSE_API_KEY set at all."""
    import run
    assert run.main(["--selftest"]) == 0
    assert "Self-test passed" in capsys.readouterr().out


def test_selftest_uses_scratch_cache_not_the_real_one(tmp_path):
    """The self-test must never write into the user's real cache/ directory."""
    from pathlib import Path
    import run
    from pipeline.config import load_settings
    import shutil

    root = Path(__file__).resolve().parent.parent
    shutil.copy(root / "config.yaml", tmp_path / "config.yaml")
    settings = load_settings(tmp_path / "config.yaml", root=tmp_path)

    scratch = run.replace_cache_dir(settings, tmp_path / "scratch")
    assert scratch.cache_dir == tmp_path / "scratch"
    assert scratch.data.provider == "synthetic"
    assert scratch.data.fallback_provider is None
    assert scratch.data.macro_fallback is None
    assert settings.data.provider == "lse", "original settings must not be mutated"


# ---------------------------------------------------------- macro discovery
def test_discover_macro_searches_every_field(monkeypatch, capsys):
    """Catalogue codes are terse ('usaeffr'); the words live in other fields."""
    import run

    catalogue = [
        {"symbol": "usaeffr", "description": "United States Effective Fed Funds Rate"},
        {"symbol": "uscpi_x", "description": "United States Consumer Price Index",
         "last_value": 324.1, "last_tick": "2026-08-01"},
        {"symbol": "chlinfind", "description": "Chile inflation"},
    ]

    class FakeProvider:
        def list_economics(self):
            return catalogue

    monkeypatch.setattr("pipeline.providers.get_provider", lambda name, settings=None: FakeProvider())
    assert run.discover_macro(settings=None, term="consumer price") == 0
    out = capsys.readouterr().out
    assert "uscpi_x" in out and "usaeffr" not in out
    assert "1 of 3 macro series matching" in out
    assert "catalogue fields:" in out
    assert "last_value=324.1" in out and "last_tick=2026-08-01" in out


def test_discover_fundamentals_prints_both_sources(monkeypatch, capsys):
    import run

    class FakeLSE:
        def fundamentals_rows(self, symbol):
            return [{"symbol": symbol, "market_cap": 1.0e12, "pe_ratio": 30.0}]

    class FakeTicker:
        def __init__(self, symbol):
            self.calendar = {"Earnings Date": ["2026-11-18"]}

    monkeypatch.setattr("pipeline.providers.get_provider", lambda name, settings=None: FakeLSE())
    monkeypatch.setattr("yfinance.Ticker", FakeTicker)
    assert run.discover_fundamentals(settings=None, symbol="NVDA") == 0
    out = capsys.readouterr().out
    assert "market_cap" in out and "1000000000000.0" in out
    assert "Earnings Date" in out and "2026-11-18" in out


def test_discover_fundamentals_reports_failures_instead_of_crashing(monkeypatch, capsys):
    import run

    def broken(name, settings=None):
        raise RuntimeError("no key")

    class BrokenTicker:
        def __init__(self, symbol):
            raise ConnectionError("offline")

    monkeypatch.setattr("pipeline.providers.get_provider", broken)
    monkeypatch.setattr("yfinance.Ticker", BrokenTicker)
    assert run.discover_fundamentals(settings=None, symbol="NVDA") == 0
    out = capsys.readouterr().out
    assert "failed: RuntimeError: no key" in out and "failed: ConnectionError: offline" in out
