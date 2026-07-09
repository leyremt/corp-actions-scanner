"""Form CB collector — foreign tender offers that surface on EDGAR.

Foreign issuers with US holders must notify the SEC of an offer made abroad via
**Form CB**. That makes EDGAR — a source this project already reads reliably
from the cloud — a window into markets whose own registries are unreachable:
SEDAR+ (Canada) sits behind a commercial bot wall, CNMV (Spain) 403s scripted
access. ~150 CB filings/year, dominated by Japanese tender offers, with Canadian
issuers mixed in.

**Noise:** Form CB also covers debt tenders (Enbridge), exchange offers and
rights offerings. We keep only equity tender offers. Filtering is by *event
type*, never by country — the observed noise is debt from Canada/Greece, while
every sampled Japanese filing was a genuine equity tender.

Classification is regex-first (free, settles ~90%); Claude Haiku is consulted
only for the ambiguous remainder, sharing `llm_extract`'s per-run call cap.

Prices: most CB filers have no US ticker, so there is often no spread — the
event is still surfaced, and the dashboard's "solo con spread" filter hides it
for anyone who only wants actionable rows. Mapping to local Yahoo symbols
(`.T` Tokyo, `.TO` Toronto) is a deliberate later step.
"""
from __future__ import annotations

import datetime as dt
import html
import re
import sys
import urllib.parse

import requests

from scanner import llm_extract
from scanner.edgar import EFTS, _get, cik_to_ticker

WINDOW_DEFAULT = 90  # CB volume is low (~12/month); a wider window than SEC's 30d

# Cover-page discriminators. Applied to the head of the document, where the
# subject securities are stated — not the whole filing, where an incidental
# "bonds" would produce a false positive.
#
# NOTE: do NOT keyword-match the offer *type* ("rights offering", "exchange
# offer", "tender offer"). Form CB's cover prints every category as a checkbox
# label, so those strings appear in EVERY filing regardless of what is checked.
# Only the free-text "securities subject to the offer" discriminates.
_HEAD = 6000
_DEBT_RE = re.compile(r"(senior notes?|debentures?|\bbonds?\b|notes due|indenture)", re.I)
_EQ_RE = re.compile(r"(common shares?|ordinary shares?|common stock)", re.I)

# EDGAR gives the country in English; the dashboard is Spanish.
_COUNTRY_ES = {
    "Japan": "Japón", "Canada": "Canadá", "Marshall Islands": "Islas Marshall",
    "Greece": "Grecia", "India": "India", "United Kingdom": "Reino Unido",
    "Switzerland": "Suiza", "Netherlands": "Países Bajos", "France": "Francia",
    "Germany": "Alemania", "Australia": "Australia", "China": "China",
    "Brazil": "Brasil", "Mexico": "México", "Spain": "España", "Israel": "Israel",
    "Bermuda": "Bermudas", "Cayman Islands": "Islas Caimán", "Ireland": "Irlanda",
    "Sweden": "Suecia", "Norway": "Noruega", "Denmark": "Dinamarca", "Italy": "Italia",
}

_country_cache: dict[int, str | None] = {}


def _country(cik: int) -> str | None:
    """Country of incorporation, in Spanish. EDGAR states it in plain text
    ('Japan', 'British Columbia, Canada'); we keep the part after the comma."""
    if cik in _country_cache:
        return _country_cache[cik]
    out = None
    try:
        j = _get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json").json()
        raw = (j.get("stateOfIncorporationDescription") or "").strip()
        if not raw:  # some filers (e.g. Wipro) leave it blank → business address
            biz = (j.get("addresses") or {}).get("business") or {}
            raw = ((biz.get("stateOrCountryDescription") or "")
                   or (biz.get("country") or "")).strip()
        if raw:
            name = raw.split(",")[-1].strip()  # "British Columbia, Canada" -> "Canada"
            out = _COUNTRY_ES.get(name, name)
    except Exception as exc:
        print(f"CB country lookup failed for CIK {cik}: {exc}", file=sys.stderr)
    _country_cache[cik] = out
    return out


def _doc_text(cik: int, adsh: str, filename: str) -> str:
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh.replace('-', '')}/{filename}"
    t = _get(url).text
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t))


def _classify(text: str) -> str:
    """'equity' | 'debt' | 'other' | 'unknown'. Regex first, Haiku on ambiguity."""
    head = text[:_HEAD]
    debt, equity = bool(_DEBT_RE.search(head)), bool(_EQ_RE.search(head))
    if equity and not debt:
        return "equity"
    if debt and not equity:
        return "debt"
    # Ambiguous: both signals, or neither. This is the ~10% the regex can't call.
    res = llm_extract.classify_offer(head)
    if not res:
        return "unknown"  # no API key, or the call failed — drop rather than guess
    if not res.get("is_tender_offer"):
        return "other"
    return res.get("security") or "unknown"


def collect(days: int = WINDOW_DEFAULT) -> list[dict]:
    """Return normalized equity tender-offer events from recent Form CB filings."""
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    params = {"q": "", "forms": "CB", "dateRange": "custom",
              "startdt": start.isoformat(), "enddt": end.isoformat()}
    try:
        hits = _get(f"{EFTS}?{urllib.parse.urlencode(params)}").json() \
                   .get("hits", {}).get("hits", [])
    except requests.RequestException as exc:
        print(f"CB search failed: {exc}", file=sys.stderr)
        return []

    # One event per issuer: CB/A amendments supersede the original CB. Dedupe
    # BEFORE downloading, so amendments don't cost an extra fetch each.
    latest: dict[int, dict] = {}
    for h in hits:
        s = h.get("_source", {})
        if not s.get("ciks"):
            continue
        cik = int(s["ciks"][0])
        prev = latest.get(cik)
        if prev is None or (s.get("file_date") or "") >= (prev["_source"].get("file_date") or ""):
            latest[cik] = h

    tickers = cik_to_ticker()
    events: list[dict] = []
    dropped: dict[str, int] = {}
    for cik, h in latest.items():
        s = h["_source"]
        adsh = s.get("adsh")
        _id = h.get("_id", "")
        _, _, filename = _id.partition(":")
        if not (adsh and filename):
            continue
        try:
            text = _doc_text(cik, adsh, filename)
        except Exception as exc:
            print(f"CB doc fetch failed for CIK {cik}: {exc}", file=sys.stderr)
            continue

        kind = _classify(text)
        if kind != "equity":
            dropped[kind] = dropped.get(kind, 0) + 1
            continue

        raw = (s.get("display_names") or ["?"])[0]
        name = re.sub(r"\s*\([^)]*\)\s*$", "", raw.split("  (CIK")[0]).strip()
        events.append({
            "id": _id,
            "source": "Foreign-CB",
            "issuer": name,
            "bidder": None,
            "cik": cik,
            "ticker": tickers.get(cik),  # usually None: foreign issuer, no US listing
            "isin": None,
            "form": s.get("form", "CB"),
            "event_type": "Foreign tender offer",
            "category": "tender",
            "announce_date": s.get("file_date"),
            "exec_date": None,  # not parsed yet (offer doc is an exhibit, often non-English)
            "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                   f"{adsh.replace('-', '')}/{filename}",
            "country": _country(cik),
        })

    print(f"CB: kept {len(events)} equity offers, dropped {dropped or '{}'}", file=sys.stderr)
    return sorted(events, key=lambda e: e["announce_date"] or "", reverse=True)


if __name__ == "__main__":
    import json

    rows = collect()
    print(f"{len(rows)} events")
    print(json.dumps(rows[:5], indent=2, ensure_ascii=False))
