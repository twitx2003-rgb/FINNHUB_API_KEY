"""SEC EDGAR Form 4 extraction. XML follows SEC's ownershipDocument schema;
names and numbers are synthetic."""
from __future__ import annotations

import json
import shutil
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline import contracts
from pipeline.cache import make_run_context
from pipeline.config import load_settings
from pipeline.errors import ConfigError, PipelineHalt, ProviderError
from pipeline.providers.sec_edgar import (
    SecClient,
    archive_url,
    cik_for,
    parse_form4,
    raw_xml_name,
    recent_filings,
    summarize,
)
from pipeline.stages import build_registry
from pipeline.stages.extract import ExtractStage

ROOT = Path(__file__).resolve().parent.parent
CIK = 1234567


def form4(transactions: str, *, plan: str | None = None, issuer: int = CIK,
          footnotes: str = "") -> str:
    plan_el = f"<aff10b5One>{plan}</aff10b5One>" if plan is not None else ""
    return f"""<?xml version="1.0"?>
<ownershipDocument>
  <schemaVersion>X0508</schemaVersion><documentType>4</documentType>
  {plan_el}
  <issuer><issuerCik>{issuer:010d}</issuerCik><issuerName>TEST CORP</issuerName>
    <issuerTradingSymbol>TEST</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerCik>0000000001</rptOwnerCik><rptOwnerName>DOE JANE</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>1</isDirector><isOfficer>true</isOfficer>
      <officerTitle>Chief Financial Officer</officerTitle><isTenPercentOwner>0</isTenPercentOwner>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>{transactions}</nonDerivativeTable>
  <footnotes>{footnotes}</footnotes>
</ownershipDocument>"""


def txn(code="S", shares="1000", price="50.25", ad="D", day="2026-09-10", after="9000",
        footnote_ref=""):
    price_el = f"<transactionPricePerShare><value>{price}</value>{footnote_ref}</transactionPricePerShare>" \
        if price is not None else "<transactionPricePerShare/>"
    return f"""<nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>{day}</value></transactionDate>
      <transactionCoding><transactionFormType>4</transactionFormType>
        <transactionCode>{code}</transactionCode><equitySwapInvolved>0</equitySwapInvolved></transactionCoding>
      <transactionAmounts><transactionShares><value>{shares}</value></transactionShares>
        {price_el}
        <transactionAcquiredDisposedCode><value>{ad}</value></transactionAcquiredDisposedCode></transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>{after}</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
      <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
    </nonDerivativeTransaction>"""


# ------------------------------------------------------------------- Form 4
def test_form4_rows_are_read_field_by_field():
    rows = parse_form4(form4(txn() + txn(code="A", shares="200", price="0", ad="A")), "0001-26-1",
                       expected_cik=CIK)
    assert len(rows) == 2
    sale = rows[0]
    assert sale["insider"] == "DOE JANE" and sale["role"] == "director, Chief Financial Officer"
    assert (sale["code"], sale["direction"], sale["shares"], sale["price"]) == ("S", "D", 1000, 50.25)
    assert sale["meaning"] == "open-market sale" and sale["shares_after"] == 9000
    assert sale["plan_10b5_1"] is False


def test_10b5_1_plan_from_the_checkbox_or_a_footnote():
    assert parse_form4(form4(txn(), plan="1"), "a")[0]["plan_10b5_1"] is True
    via_note = form4(txn(footnote_ref='<footnoteId id="F1"/>'),
                     footnotes='<footnote id="F1">Sold under a Rule 10b5-1 trading plan adopted in March.</footnote>')
    assert parse_form4(via_note, "a")[0]["plan_10b5_1"] is True


def test_missing_price_is_kept_as_unknown_not_zero():
    row = parse_form4(form4(txn(price=None)), "a")[0]
    assert row["price"] is None
    assert summarize([row])["open_market_rows_without_price"] == 1


@pytest.mark.parametrize("xml,match", [
    ("<notXml", "not valid XML"),
    ("<other/>", "expected <ownershipDocument>"),
    (form4(txn(shares="")), "missing transactionAmounts/transactionShares"),
    (form4(txn(shares="many")), "not a number"),
    (form4(txn(ad="X")), "missing code, date or A/D"),
    (form4(txn(), issuer=999), "expected 1234567"),
])
def test_unreadable_form4_fails_loudly(xml, match):
    with pytest.raises(ProviderError, match=match):
        parse_form4(xml, "0001-26-1", expected_cik=CIK)


def test_summary_separates_open_market_trades_from_grants():
    rows = parse_form4(form4(txn(code="S", shares="1000", price="10") +
                             txn(code="S", shares="3000", price="10") +
                             txn(code="P", shares="500", price="8", ad="A") +
                             txn(code="F", shares="100", price="10")), "a")
    rows[1]["plan_10b5_1"] = True
    s = summarize(rows)
    assert s["open_market_sales"] == {"count": 2, "shares": 4000, "value_usd": 40000}
    assert s["open_market_buys"] == {"count": 1, "shares": 500, "value_usd": 4000}
    assert s["sale_shares_under_10b5_1_pct"] == 75.0 and s["other_codes"] == {"F": 1}


# ------------------------------------------------------------------ filings
def test_cik_lookup_and_recent_filings():
    assert cik_for({"0": {"cik_str": CIK, "ticker": "TEST", "title": "x"}}, "test") == CIK
    with pytest.raises(ProviderError, match="0 entries"):
        cik_for({"0": {"cik_str": 1, "ticker": "OTHER"}}, "TEST")
    subs = {"filings": {"recent": {
        "accessionNumber": ["a1", "a2", "a3"], "form": ["4", "10-Q", "4"],
        "filingDate": ["2026-09-01", "2026-08-01", "2026-01-01"],
        "primaryDocument": ["xslF345X05/f1.xml", "q.htm", "xslF345X05/f3.xml"]}}}
    got = recent_filings(subs, {"4"}, date(2026, 6, 1))
    assert [f.accession for f in got] == ["a1"]


def test_ragged_submissions_are_refused():
    subs = {"filings": {"recent": {"accessionNumber": ["a"], "form": ["4", "4"],
                                   "filingDate": ["2026-09-01"], "primaryDocument": ["x.xml"]}}}
    with pytest.raises(ProviderError, match="differ in length"):
        recent_filings(subs, {"4"}, date(2026, 1, 1))


def test_raw_xml_path_drops_the_xsl_view_folder():
    assert raw_xml_name("xslF345X05/wk-form4_1.xml") == "wk-form4_1.xml"
    with pytest.raises(ProviderError, match="not XML"):
        raw_xml_name("form4.htm")


# ------------------------------------------------------------------- client
def test_client_requires_a_contact_user_agent(tmp_path):
    with pytest.raises(ConfigError, match="SEC_USER_AGENT"):
        SecClient("", tmp_path)
    with pytest.raises(ConfigError):
        SecClient("just a name", tmp_path)


def test_client_sends_the_user_agent_waits_between_calls_and_caches(tmp_path):
    calls, slept = [], []

    def get(url, headers, timeout):
        calls.append((url, headers["User-Agent"]))
        return SimpleNamespace(status_code=200, text="{}")

    client = SecClient("Jane Doe jane@example.com", tmp_path, get=get, sleep=slept.append)
    client.fetch("https://x/1", cache=True)
    client.fetch("https://x/1", cache=True)          # cached: no second request
    client.fetch("https://x/2", cache=False)
    assert calls == [("https://x/1", "Jane Doe jane@example.com"),
                     ("https://x/2", "Jane Doe jane@example.com")]
    assert slept and all(s <= 0.15 for s in slept)


def test_403_explains_the_user_agent_rule(tmp_path):
    client = SecClient("a b@c.d", tmp_path,
                       get=lambda *a, **k: SimpleNamespace(status_code=403, text=""))
    with pytest.raises(ProviderError, match="User-Agent"):
        client.fetch("https://x", cache=False)


# --------------------------------------------------------------------- stage
class FakeSec:
    def __init__(self, documents):
        self.documents = documents

    def json(self, url, cache=False):
        return json.loads(self.fetch(url, cache=cache))

    def fetch(self, url, cache):
        for key, body in self.documents.items():
            if key in url:
                return body
        raise ProviderError(f"unexpected url {url}")


@pytest.fixture
def ctx(tmp_path):
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    c = make_run_context(load_settings(tmp_path / "config.yaml", root=tmp_path), "TEST", "2026-09-22")
    c.write_json("validation", {"status": "pass", "checks": []})
    return c


def sec_documents(first_xml):
    recent = (date.today() - timedelta(days=5)).isoformat()
    old = (date.today() - timedelta(days=400)).isoformat()
    subs = {"filings": {"recent": {
        "accessionNumber": ["0001-26-000001", "0001-26-000002", "0001-25-000009"],
        "form": ["4", "4/A", "4"], "filingDate": [recent, recent, old],
        "primaryDocument": ["xslF345X05/a.xml", "xslF345X05/b.xml", "xslF345X05/c.xml"]}}}
    return {"company_tickers.json": json.dumps({"0": {"cik_str": CIK, "ticker": "TEST"}}),
            "CIK0001234567.json": json.dumps(subs),
            "/1234567/000126000001/a.xml": first_xml}


def test_extract_stage_writes_transactions_and_summary(ctx):
    stage = ExtractStage(lambda c: FakeSec(sec_documents(form4(txn() + txn(code="P", ad="A")))))
    stage._gate(ctx)
    result = stage.run(ctx)
    df = ctx.read_parquet("extract_insider", contracts.INSIDER)
    assert len(df) == 2 and set(df["code"]) == {"S", "P"}
    report = ctx.read_json("extract_insider")
    assert report["filings_read"] == 1 and report["amendments_not_read"] == 1
    assert "1 open-market buys, 1 sales" in result.summary


def test_extract_stage_fails_on_an_unreadable_filing(ctx):
    stage = ExtractStage(lambda c: FakeSec(sec_documents("<broken")))
    with pytest.raises(ProviderError, match="not valid XML"):
        stage.run(ctx)


def test_extract_stage_is_behind_the_validation_gate(tmp_path):
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    c = make_run_context(load_settings(tmp_path / "config.yaml", root=tmp_path), "TEST", "2026-09-22")
    with pytest.raises(PipelineHalt):
        build_registry()["extract"]._gate(c)


def test_archive_url_uses_the_raw_xml():
    from pipeline.providers.sec_edgar import Filing
    f = Filing("0001045810-26-000123", "4", date(2026, 9, 1), "xslF345X05/wk-form4_9.xml")
    assert archive_url(1045810, f) == ("https://www.sec.gov/Archives/edgar/data/1045810/"
                                       "000104581026000123/wk-form4_9.xml")


# ------------------------------------------------------- asking for the UA
def _settings(tmp_path, monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    (tmp_path / ".env").write_text("LSE_API_KEY=x", encoding="utf-8")     # no trailing newline
    return load_settings(tmp_path / "config.yaml", root=tmp_path)


def test_missing_user_agent_is_asked_for_and_saved(tmp_path, monkeypatch, capsys):
    settings = _settings(tmp_path, monkeypatch)
    got = settings.env_or_ask("SEC_USER_AGENT", "q?", must_contain="@",
                              ask=lambda prompt: "  Jane Doe jane@example.com ", is_interactive=True)
    assert got == "Jane Doe jane@example.com"
    assert (tmp_path / ".env").read_text(encoding="utf-8") == \
        "LSE_API_KEY=x\nSEC_USER_AGENT=Jane Doe jane@example.com\n"
    # asked once: now it comes from the environment
    assert settings.env_or_ask("SEC_USER_AGENT", "q?", ask=lambda p: pytest.fail("asked again"),
                               is_interactive=True) == got


def test_bad_answer_saves_nothing(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    with pytest.raises(ConfigError, match="Nothing was saved"):
        settings.env_or_ask("SEC_USER_AGENT", "q?", must_contain="@",
                            ask=lambda p: "Jane Doe", is_interactive=True)
    assert "SEC_USER_AGENT" not in (tmp_path / ".env").read_text(encoding="utf-8")


def test_no_terminal_means_no_question(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    with pytest.raises(ConfigError, match="not set"):
        settings.env_or_ask("SEC_USER_AGENT", "q?", ask=lambda p: pytest.fail("asked"),
                            is_interactive=False)
