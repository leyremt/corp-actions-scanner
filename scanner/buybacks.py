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

import datetime as dt
import html
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


_DE_MONTHS = {"januar": 1, "februar": 2, "märz": 3, "april": 4, "mai": 5, "juni": 6,
              "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12}
_DE_DATE = r"(\d{1,2})\.\s*(Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)\s*(\d{4})"
_PERIOD_RE = re.compile(r"(?:Annahmefrist|Erwerbsfrist|Angebotsfrist)", re.IGNORECASE)
_BIS_RE = re.compile(rf"bis\s+(?:zum\s+)?{_DE_DATE}", re.IGNORECASE)


def _de_date_iso(day: str, month: str, year: str) -> str | None:
    mo = _DE_MONTHS.get(month.lower())
    return f"{int(year):04d}-{mo:02d}-{int(day):02d}" if mo else None


def _period_from_article(link: str, after: str | None) -> str | None:
    try:
        html_txt = _session.get(link, timeout=25).text
        time.sleep(0.15)
    except requests.RequestException:
        return None
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", html_txt)))
    for km in _PERIOD_RE.finditer(text):
        bm = _BIS_RE.search(text[km.start():km.start() + 220])
        if bm:
            iso = _de_date_iso(*bm.groups())
            if iso and (not after or iso >= after):
                return iso
    return None


def _bing_links(query: str) -> list[str]:
    try:
        r = _session.get(
            f"https://www.bing.com/news/search?q={urllib.parse.quote(query)}&format=rss&setlang=de&cc=DE",
            timeout=30)
        time.sleep(0.2)
        return [it.findtext("link") for it in ET.fromstring(r.content).findall(".//item")]
    except (requests.RequestException, ET.ParseError):
        return []


def _acceptance_end(links: list[str], name: str, after: str | None) -> str | None:
    """Best-effort acceptance-period end date (Annahmefrist ... bis X). First
    reuse the already-gathered article links; if none yield a date, run one
    targeted news search for this company. Only Bing 'apiclick' links resolve
    via HTTP (Google News links are JS redirects)."""
    candidates = [l for l in links if l and "bing.com/news/apiclick" in l]
    candidates += _bing_links(f'"{name}" Rückkaufangebot Annahmefrist')
    seen: set[str] = set()
    for link in candidates:
        if not link or "apiclick" not in link or link in seen:
            continue
        seen.add(link)
        if len(seen) > 6:
            break
        iso = _period_from_article(link, after)
        if iso:
            return iso
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


# Query two news RSS feeds and merge: Google News is blocked from datacenter
# IPs (returns empty in GitHub Actions), Bing News works there — using both
# makes the source robust whether it runs locally or in the cloud.
_FEEDS = [
    "https://news.google.com/rss/search?q={q}&hl=de&gl=DE&ceid=DE:de",
    "https://www.bing.com/news/search?q={q}&format=rss&setlang=de&cc=DE",
]


def _rss(keyword: str) -> list[dict]:
    q = urllib.parse.quote(f'"{keyword}"')
    items = []
    for tmpl in _FEEDS:
        try:
            r = _session.get(tmpl.format(q=q), timeout=30)
            time.sleep(0.2)
            items += ET.fromstring(r.content).findall(".//item")
        except (requests.RequestException, ET.ParseError):
            continue
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


def collect(days: int = 90) -> list[dict]:
    # A buyback acceptance period runs a few weeks; without the offer document we
    # can't read its end date, so we treat anything announced longer than `days`
    # ago as closed and drop it (also filters stale/recurring old news items).
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    raw = [it for kw in KEYWORDS for it in _rss(kw)]

    # Resolve each article to a ticker (cached), then merge by ticker so that
    # name variants ("Dermapharm" vs "Dermapharm Holding SE") collapse into one
    # event with the earliest announce date and the highest stated offer price.
    sym_cache: dict[str, str | None] = {}
    merged: dict[str, dict] = {}
    for it in raw:
        iso = _iso(it["pubdate"])
        if not iso or iso < cutoff:        # skip undated / stale (likely closed) offers
            continue
        name = _company(it["title"])
        if len(name) < 2:
            continue
        if name.lower() not in sym_cache:
            sym_cache[name.lower()] = resolve_symbol(name)
        symbol = sym_cache[name.lower()]
        key = symbol or name.lower()
        price = _price(it["title"])
        rec = merged.get(key)
        if rec is None:
            merged[key] = {"name": name, "ticker": symbol, "announce_date": iso,
                           "offer_price": price, "link": it["link"], "links": [it["link"]]}
        else:
            rec["links"].append(it["link"])
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
        # Best-effort acceptance-period end date scraped from news articles.
        "exec_date": _acceptance_end(rec.get("links", []), rec["name"], rec["announce_date"]),
        "offer_price": rec["offer_price"],
        "url": rec["link"],
    } for key, rec in merged.items()]


if __name__ == "__main__":
    import json
    rows = collect()
    print(f"{len(rows)} buyback events")
    print(json.dumps(rows, indent=2, ensure_ascii=False))
