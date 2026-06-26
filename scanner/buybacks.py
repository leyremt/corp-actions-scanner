"""German issuer share-buyback / self-tender collector.

These public buyback offers (öffentliche Aktienrückkaufangebote — a company
tendering for its own shares at a fixed price, e.g. Aumann at EUR 17.80) are a
core odd-lot arbitrage category, but they are NOT WpÜG offers, so they never
appear in BaFin's takeover databases. There is no central registry: they are
disclosed via ad-hoc news (EQS/DGAP). We therefore source them from Google
News RSS filtered on the standard German terms, then resolve the company name
to a Yahoo symbol for a live price.

News-derived → lower precision than SEC/BaFin. Labelled source "DE-Buyback".
"""
from __future__ import annotations

import re
import time
import urllib.parse
import xml.etree.ElementTree as ET

import requests

import os

UA = f"Mozilla/5.0 (corp-actions-scanner; {os.environ.get('SEC_CONTACT', 'corp-actions-scanner@users.noreply.github.com')})"
KEYWORDS = [
    "Aktienrückkaufangebot",
    "Erwerbsangebot eigener Aktien",
    "öffentliches Rückkaufangebot",
]
# Title must look like an actual buyback OFFER, not generic buyback chatter.
_OFFER_RE = re.compile(r"r[üu]ckkaufangebot|erwerbsangebot eigener", re.IGNORECASE)
# Strip wire prefixes and trailing " - <source>".
_PREFIX_RE = re.compile(
    r"^(EQS-?(Adhoc|News)|DGAP-?(Adhoc|News)|Original-Research|PTA-?\w+|[A-Za-zäöüÄÖÜ ]{0,25}Börse)\s*:\s*",
    re.I,
)
_VERB_RE = re.compile(
    r"\s+(beschließt|startet|lanciert|bietet|kündigt|plant|verlängert|erhöht|legt|begibt|veröffentlicht)\b",
    re.IGNORECASE,
)
_PRICE_RE = re.compile(r"(?:€|EUR)\s*([0-9]{1,4}(?:[.,][0-9]{2})?)|([0-9]{1,4}(?:[.,][0-9]{2})?)\s*(?:€|EUR|Euro)")

_session = requests.Session()
_session.headers.update({"User-Agent": UA})


def _de_num(s: str) -> float | None:
    try:
        return float(s.replace(".", "").replace(",", "."))
    except (ValueError, AttributeError):
        return None


def _company(title: str) -> str:
    t = _PREFIX_RE.sub("", title).strip()
    t = re.split(r"\s+[-–]\s+\S", t)[0]  # drop " - MarketScreener.de" style suffix
    m = _VERB_RE.search(t)
    name = (t[: m.start()] if m else " ".join(t.split()[:4])).strip(" .:-")
    return name


def _price(title: str) -> float | None:
    best = None
    for m in _PRICE_RE.finditer(title):
        v = _de_num(m.group(1) or m.group(2))
        if v and 0.2 <= v <= 5000:
            best = v if best is None else max(best, v)
    return best


def _rss(keyword: str) -> list[dict]:
    q = urllib.parse.quote(f'"{keyword}"')
    url = f"https://news.google.com/rss/search?q={q}&hl=de&gl=DE&ceid=DE:de"
    try:
        r = _session.get(url, timeout=30)
        time.sleep(0.2)
        items = ET.fromstring(r.content).findall(".//item")
    except (requests.RequestException, ET.ParseError):
        return []
    out = []
    for it in items:
        title = (it.findtext("title") or "").strip()
        if not _OFFER_RE.search(title):
            continue
        out.append({
            "title": title,
            "link": it.findtext("link") or "",
            "pubdate": it.findtext("pubDate") or "",
        })
    return out


def _iso(pubdate: str) -> str | None:
    # RFC-822: "Fri, 05 Jun 2026 10:00:00 GMT"
    m = re.search(r"(\d{1,2})\s+(\w{3})\s+(\d{4})", pubdate)
    if not m:
        return None
    months = {m: i for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
    mon = months.get(m.group(2))
    return f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(1)):02d}" if mon else None


def resolve_symbol(name: str) -> str | None:
    """Company name -> Yahoo symbol, preferring XETRA / German listings."""
    try:
        r = _session.get(
            "https://query2.finance.yahoo.com/v1/finance/search",
            params={"q": name, "quotesCount": 6, "newsCount": 0},
            timeout=20,
        )
        time.sleep(0.2)
        quotes = [q for q in r.json().get("quotes", []) if q.get("symbol")]
    except (requests.RequestException, ValueError):
        return None
    if not quotes:
        return None
    order = {".DE": 0, ".F": 1, ".MU": 2, ".HM": 3, ".SG": 4}
    quotes.sort(key=lambda q: order.get(re.search(r"\.\w+$", q["symbol"]).group(0)
                                        if re.search(r"\.\w+$", q["symbol"]) else "", 9))
    return quotes[0]["symbol"]


def collect() -> list[dict]:
    raw = [it for kw in KEYWORDS for it in _rss(kw)]

    # Resolve each article to a ticker (cached), then merge by ticker so that
    # name variants ("Dermapharm" vs "Dermapharm Holding SE") collapse into one
    # event with the earliest announce date and the highest stated offer price.
    sym_cache: dict[str, str | None] = {}
    merged: dict[str, dict] = {}
    for it in raw:
        name = _company(it["title"])
        if len(name) < 2:
            continue
        if name.lower() not in sym_cache:
            sym_cache[name.lower()] = resolve_symbol(name)
        symbol = sym_cache[name.lower()]
        key = symbol or name.lower()
        iso, price = _iso(it["pubdate"]), _price(it["title"])
        rec = merged.get(key)
        if rec is None:
            merged[key] = {"name": name, "ticker": symbol, "announce_date": iso,
                           "offer_price": price, "link": it["link"]}
        else:
            if len(name) < len(rec["name"]):           # prefer the shorter, cleaner name
                rec["name"] = name
            if iso and (rec["announce_date"] is None or iso < rec["announce_date"]):
                rec["announce_date"] = iso
            if price and (rec["offer_price"] is None or price > rec["offer_price"]):
                rec["offer_price"] = price

    return [{
        "id": f"buyback:{key}",
        "source": "DE-Buyback",
        "issuer": rec["name"],
        "bidder": None,
        "cik": None,
        "ticker": rec["ticker"],
        "isin": None,
        "form": "Share buyback offer",
        "event_type": "Buyback offer",
        "category": "tender",
        "announce_date": rec["announce_date"],
        "exec_date": None,  # acceptance period not in the news title
        "offer_price": rec["offer_price"],
        "url": rec["link"],
    } for key, rec in merged.items()]


if __name__ == "__main__":
    import json
    rows = collect()
    print(f"{len(rows)} buyback events")
    print(json.dumps(rows, indent=2, ensure_ascii=False))
