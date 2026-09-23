"""SEC EDGAR: insider transactions (Form 4) for a ticker.

Official, free, public-domain data with a documented API — so it is read
directly, not scraped. SEC's fair-access policy requires every request to carry
a User-Agent naming who you are with a contact e-mail, and at most 10 requests
per second. Scrapling (built to look like a browser) is the wrong tool here;
it stays reserved for sites without an API.

Endpoints (documented by SEC; not reachable from the build machine, so each
shape is checked strictly and `run.py --discover-sec` shows the live one):
    https://www.sec.gov/files/company_tickers.json
        {"0": {"cik_str": 1045810, "ticker": "NVDA", "title": "..."}, ...}
    https://data.sec.gov/submissions/CIK##########.json
        {"cik", "name", "filings": {"recent": {"accessionNumber": [...],
         "filingDate": [...], "form": [...], "primaryDocument": [...], ...}}}
    https://www.sec.gov/Archives/edgar/data/<cik>/<accession-no-dashes>/<file>.xml
        Form 4 "ownershipDocument" XML (primaryDocument points at the XSL-styled
        view "xslF345X05/<file>.xml"; the raw XML is the same file name at the
        folder root).
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

from ..errors import ConfigError, ProviderError

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{name}"
MIN_INTERVAL = 0.15            # seconds between requests: under SEC's 10/second

# Form 4 transaction codes (SEC Form 4 instructions, General Instruction 8)
CODE_MEANING = {
    "P": "open-market purchase", "S": "open-market sale", "A": "grant or award",
    "M": "option exercise", "F": "shares withheld for tax", "G": "gift",
    "C": "conversion", "D": "sale back to issuer", "X": "option exercise (in the money)",
    "J": "other", "W": "inheritance", "I": "discretionary",
}


class SecClient:
    """Rate-limited GET with the declared User-Agent. Archive documents never
    change once filed, so they are cached on disk for good."""

    def __init__(self, user_agent: str, cache_dir: Path, *,
                 get: Callable[..., Any] | None = None, sleep: Callable[[float], None] = time.sleep):
        if not user_agent or "@" not in user_agent:
            raise ConfigError("SEC_USER_AGENT must be set in .env as \"Your Name your@email\" — "
                              "SEC requires a contact in every request (fair-access policy)")
        self.headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        self.cache_dir = cache_dir
        self._sleep = sleep
        self._last = 0.0
        if get is None:
            import requests
            get = requests.get
        self._get = get

    def fetch(self, url: str, *, cache: bool) -> str:
        path = self.cache_dir / (hashlib.sha256(url.encode()).hexdigest()[:24] + ".txt")
        if cache and path.exists():
            return path.read_text(encoding="utf-8")
        wait = MIN_INTERVAL - (time.monotonic() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = time.monotonic()
        try:
            response = self._get(url, headers=self.headers, timeout=30)
        except Exception as exc:  # noqa: BLE001 — network errors are provider errors
            raise ProviderError(f"SEC request failed: {url}: {exc}") from exc
        if response.status_code != 200:
            hint = (" — SEC refuses requests without a proper User-Agent; check SEC_USER_AGENT"
                    if response.status_code == 403 else "")
            raise ProviderError(f"SEC answered {response.status_code} for {url}{hint}")
        text = response.text
        if cache:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return text

    def json(self, url: str, *, cache: bool = False) -> Any:
        try:
            return json.loads(self.fetch(url, cache=cache))
        except ValueError as exc:
            raise ProviderError(f"SEC returned non-JSON for {url}") from exc


# ------------------------------------------------------------------ filings
def cik_for(tickers_payload: Any, ticker: str) -> int:
    if not isinstance(tickers_payload, dict):
        raise ProviderError("company_tickers.json: expected an object")
    matches = [row for row in tickers_payload.values()
               if isinstance(row, dict) and str(row.get("ticker", "")).upper() == ticker.upper()]
    if len(matches) != 1:
        raise ProviderError(f"company_tickers.json: {len(matches)} entries for {ticker}")
    if "cik_str" not in matches[0]:
        raise ProviderError(f"company_tickers.json: no cik_str for {ticker}. "
                            f"Keys: {sorted(matches[0])}")
    return int(matches[0]["cik_str"])


@dataclass(frozen=True)
class Filing:
    accession: str
    form: str
    filed: date
    primary_document: str


def recent_filings(submissions: Any, forms: set[str], since: date) -> list[Filing]:
    """Rows of the column-oriented `filings.recent` block, newest first."""
    try:
        recent = submissions["filings"]["recent"]
    except (KeyError, TypeError):
        raise ProviderError("submissions JSON: no filings.recent block. "
                            f"Top-level keys: {sorted(submissions) if isinstance(submissions, dict) else '?'}") from None
    columns = ("accessionNumber", "form", "filingDate", "primaryDocument")
    missing = [c for c in columns if c not in recent]
    if missing:
        raise ProviderError(f"submissions JSON: filings.recent lacks {missing}. Keys: {sorted(recent)}")
    lengths = {len(recent[c]) for c in columns}
    if len(lengths) != 1:
        raise ProviderError(f"submissions JSON: filings.recent columns differ in length {lengths}")
    out = []
    for acc, form, filed, doc in zip(*(recent[c] for c in columns)):
        day = date.fromisoformat(filed)
        if form in forms and day >= since:
            out.append(Filing(acc, form, day, doc))
    return sorted(out, key=lambda f: f.filed, reverse=True)


def raw_xml_name(primary_document: str) -> str:
    """'xslF345X05/wk-form4_1.xml' -> 'wk-form4_1.xml' (the XSL view -> the raw XML)."""
    name = primary_document.rsplit("/", 1)[-1]
    if not name.lower().endswith(".xml"):
        raise ProviderError(f"Form 4 primary document is not XML: {primary_document}")
    return name


def archive_url(cik: int, filing: Filing) -> str:
    return ARCHIVE_URL.format(cik=cik, folder=filing.accession.replace("-", ""),
                              name=raw_xml_name(filing.primary_document))


# ------------------------------------------------------------------- Form 4
def _text(node, path: str) -> str | None:
    found = node.find(path)
    if found is None or found.text is None:
        return None
    value = found.text.strip()
    return value or None


def _flag(node, path: str) -> bool:
    return (_text(node, path) or "0").lower() in ("1", "true")


def _number(node, path: str, context: str, *, required: bool) -> float | None:
    raw = _text(node, path)
    if raw is None:
        if required:
            raise ProviderError(f"{context}: missing {path}")
        return None
    try:
        return float(raw)
    except ValueError:
        raise ProviderError(f"{context}: {path} is not a number: {raw!r}") from None


def parse_form4(xml_text: str, accession: str, expected_cik: int | None = None) -> list[dict]:
    """Non-derivative transactions of one Form 4, one row each."""
    from lxml import etree

    context = f"Form 4 {accession}"
    try:
        root = etree.fromstring(xml_text.encode("utf-8"))
    except etree.XMLSyntaxError as exc:
        raise ProviderError(f"{context}: not valid XML ({exc})") from exc
    if root.tag != "ownershipDocument":
        raise ProviderError(f"{context}: root is <{root.tag}>, expected <ownershipDocument>")

    issuer = _text(root, "issuer/issuerCik")
    if expected_cik is not None and (issuer is None or int(issuer) != expected_cik):
        raise ProviderError(f"{context}: filed for issuer CIK {issuer}, expected {expected_cik}")

    owners = root.findall("reportingOwner")
    if not owners:
        raise ProviderError(f"{context}: no reportingOwner")
    names = [_text(o, "reportingOwnerId/rptOwnerName") for o in owners]
    if not all(names):
        raise ProviderError(f"{context}: reporting owner without a name")
    rel = owners[0].find("reportingOwnerRelationship")
    roles = []
    if rel is not None:
        if _flag(rel, "isDirector"):
            roles.append("director")
        if _flag(rel, "isOfficer"):
            roles.append(_text(rel, "officerTitle") or "officer")
        if _flag(rel, "isTenPercentOwner"):
            roles.append("10% owner")
        if _flag(rel, "isOther"):
            roles.append(_text(rel, "otherText") or "other")

    footnotes = {f.get("id"): " ".join(f.itertext()) for f in root.findall("footnotes/footnote")}
    plan_flag = _flag(root, "aff10b5One")

    rows = []
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _text(t, "transactionCoding/transactionCode")
        day = _text(t, "transactionDate/value")
        direction = _text(t, "transactionAmounts/transactionAcquiredDisposedCode/value")
        if not code or not day or direction not in ("A", "D"):
            raise ProviderError(f"{context}: transaction missing code, date or A/D "
                                f"({code!r}, {day!r}, {direction!r})")
        refs = [f.get("id") for f in t.iter("footnoteId")]
        plan = plan_flag or any("10b5-1" in footnotes.get(r, "") for r in refs)
        rows.append({
            "accession": accession,
            "insider": "; ".join(names),
            "role": ", ".join(roles) or "not stated",
            "date": day[:10],
            "code": code,
            "meaning": CODE_MEANING.get(code, "other"),
            "direction": direction,
            "shares": _number(t, "transactionAmounts/transactionShares/value", context, required=True),
            "price": _number(t, "transactionAmounts/transactionPricePerShare/value", context,
                             required=False),
            "shares_after": _number(t, "postTransactionAmounts/sharesOwnedFollowingTransaction/value",
                                    context, required=False),
            "ownership": _text(t, "ownershipNature/directOrIndirectOwnership/value") or "?",
            "plan_10b5_1": plan,
        })
    return rows


def summarize(rows: list[dict]) -> dict:
    """What a reader wants first: open-market buying vs selling, and how much
    of the selling was pre-scheduled under 10b5-1 plans."""
    def side(code: str) -> dict:
        picked = [r for r in rows if r["code"] == code]
        return {"count": len(picked),
                "shares": sum(r["shares"] for r in picked),
                "value_usd": round(sum(r["shares"] * (r["price"] or 0) for r in picked), 2)}

    sales = [r for r in rows if r["code"] == "S"]
    sale_shares = sum(r["shares"] for r in sales)
    planned = sum(r["shares"] for r in sales if r["plan_10b5_1"])
    return {
        "transactions": len(rows),
        "insiders": len({r["insider"] for r in rows}),
        "open_market_buys": side("P"),
        "open_market_sales": side("S"),
        "sale_shares_under_10b5_1_pct": round(100 * planned / sale_shares, 1) if sale_shares else None,
        "other_codes": {c: sum(r["code"] == c for r in rows)
                        for c in sorted({r["code"] for r in rows} - {"P", "S"})},
        # A sale with no price reported is counted in shares but not in dollars.
        "open_market_rows_without_price": sum(1 for r in rows
                                              if r["code"] in ("P", "S") and r["price"] is None),
    }
