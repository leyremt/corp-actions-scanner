"""German squeeze-out / delisting / BuG collector via the SpruchZ blog feed.

Proper squeeze-outs (§327a AktG), Delisting announcements and Beherrschungs-
und Gewinnabführungsverträge run through shareholder meetings and the
Bundesanzeiger — which has no API and hostile anti-bot measures. The SpruchZ
blog (spruchverfahren.blogspot.com) tracks every German squeeze-out,
delisting and Spruchverfahren as it is announced, and Blogspot exposes a
clean JSON feed. We use it as the practical proxy for Phase 3.

News-derived → announce date is the post date; offer price (Barabfindung) is
extracted best-effort from the post body. Labelled source "DE-SqueezeOut".
"""
from __future__ import annotations

import datetime as dt
import html
import os
import re
import time

import requests

UA = f"Mozilla/5.0 (corp-actions-scanner; {os.environ.get('SEC_CONTACT', 'corp-actions-scanner@users.noreply.github.com')})"
FEED = "https://spruchverfahren.blogspot.com/feeds/posts/default"

# Actionable title patterns -> (event label, category). Order matters: the
# post-deal court phase (Spruchverfahren) is matched first so it doesn't get
# classified as a fresh squeeze-out.
_TYPES = [
    (re.compile(r"spruchverfahren|nachprüfungsverfahren|nachbesserung", re.I),
     ("Spruchverfahren", "squeeze_out")),
    (re.compile(r"squeeze[- ]?out", re.I), ("Squeeze-out", "squeeze_out")),
    (re.compile(r"delisting", re.I), ("Delisting (DE)", "delisting")),
    (re.compile(r"beherrschungs|gewinnabführungsvertrag", re.I),
     ("BuG-Vertrag", "squeeze_out")),
]
# Generic articles, candidate lists, foreign-law essays — not single events.
_SKIP_RE = re.compile(
    r"kandidaten|übersicht|in den niederlanden|in luxemburg|in österreich|"
    r"in der schweiz|literatur|rückblick|buchbesprechung|veranstaltung",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
# The extracted "company" must actually look like one (not a magazine name or
# a mis-parsed phrase).
_LOOKS_COMPANY = re.compile(
    r"\b(AG|SE|KGaA|Aktiengesellschaft|GmbH|N\.V\.|S\.A\.|SE & Co)\b")
_NOT_COMPANY = re.compile(
    r"^(spruchverfahren|nachprüfungsverfahren|squeeze|delisting|beherrschung|"
    r"verschmelzung|übernahme|anfechtungsklage|stellungnahme)", re.IGNORECASE)
_PRICE_RE = re.compile(
    r"(?:barabfindung|abfindung|ausgleich|gegenleistung)[^.]{0,120}?"
    r"([0-9]{1,4}(?:\.[0-9]{3})*,[0-9]{2})\s*(?:EUR|€|Euro)|"
    r"(?:EUR|€)\s*([0-9]{1,4}(?:\.[0-9]{3})*,[0-9]{2})[^.]{0,60}?je\s+(?:Stück)?[Aa]ktie",
    re.IGNORECASE,
)

_session = requests.Session()
_session.headers.update({"User-Agent": UA})


def _de_num(s: str) -> float | None:
    try:
        return float(s.replace(".", "").replace(",", "."))
    except (ValueError, AttributeError):
        return None


def _company(title: str) -> str | None:
    """Best-effort company name from a SpruchZ post title."""
    m = re.search(r"\bbei\s+der\s+(.+?)(?::|$)", title)
    if m:
        return m.group(1).strip(" .")
    m = re.match(r"([^:]{3,70}?(?:AG|SE|KGaA|GmbH & Co\. KGaA))\s*:", title)
    if m:
        return m.group(1).strip()
    m = re.match(r"([^:]{3,70}):\s", title)
    if m:
        return m.group(1).strip()
    return None


def _classify(title: str) -> tuple[str, str] | None:
    if _SKIP_RE.search(title):
        return None
    for pattern, label_cat in _TYPES:
        if pattern.search(title):
            return label_cat
    return None


def _fetch(max_results: int = 150) -> list[dict]:
    try:
        r = _session.get(FEED, params={"alt": "json", "max-results": max_results}, timeout=30)
        time.sleep(0.2)
        return r.json().get("feed", {}).get("entry", [])
    except (requests.RequestException, ValueError):
        return []


def collect(days: int = 120) -> list[dict]:
    """Normalized squeeze-out/delisting events from recent SpruchZ posts."""
    from scanner.buybacks import resolve_symbol  # reuse Yahoo name->symbol

    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    merged: dict[str, dict] = {}
    sym_cache: dict[str, str | None] = {}

    for entry in _fetch():
        title = html.unescape(entry.get("title", {}).get("$t", "")).strip()
        published = entry.get("published", {}).get("$t", "")[:10]
        if not published or published < cutoff:
            continue
        label_cat = _classify(title)
        if not label_cat:
            continue
        name = _company(title)
        if (not name or len(name) < 3 or not _LOOKS_COMPANY.search(name)
                or _NOT_COMPANY.match(name)):
            continue  # drops magazine mentions and failed extractions
        body = _TAG_RE.sub(" ", html.unescape(entry.get("content", {}).get("$t", "")))
        price = None
        pm = _PRICE_RE.search(body)
        if pm:
            price = _de_num(pm.group(1) or pm.group(2))
        link = next((l["href"] for l in entry.get("link", []) if l.get("rel") == "alternate"), "")

        key = name.lower()
        rec = merged.get(key)
        if rec is None:
            merged[key] = {"name": name, "label": label_cat[0], "category": label_cat[1],
                           "announce_date": published, "offer_price": price, "link": link}
        else:
            # Keep the earliest date, the freshest known price, and prefer the
            # concrete deal label over the court-phase one.
            if published < rec["announce_date"]:
                rec["announce_date"] = published
            if price and rec["offer_price"] is None:
                rec["offer_price"] = price
            if rec["label"] == "Spruchverfahren" and label_cat[0] != "Spruchverfahren":
                rec["label"], rec["category"] = label_cat

    events = []
    for key, rec in merged.items():
        if rec["name"].lower() not in sym_cache:
            sym_cache[rec["name"].lower()] = resolve_symbol(rec["name"])
        events.append({
            "id": f"squeezeout:{key}",
            "source": "DE-SqueezeOut",
            "issuer": rec["name"],
            "bidder": None,
            "cik": None,
            "ticker": sym_cache[rec["name"].lower()],
            "isin": None,
            "form": rec["label"],
            "event_type": rec["label"],
            "category": rec["category"],
            "announce_date": rec["announce_date"],
            "exec_date": None,  # executes at HV registration — not in the feed
            "offer_price": rec["offer_price"],
            "url": rec["link"],
        })
    return events


if __name__ == "__main__":
    import json
    rows = collect()
    print(f"{len(rows)} squeeze-out events")
    for r in rows:
        print(f"  {r['announce_date']} {r['event_type']:16} {str(r['ticker']):10} "
              f"price={r['offer_price']}  {r['issuer'][:40]}")
