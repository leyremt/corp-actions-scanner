"""Enrichment: current market price (yfinance) + best-effort offer-price
extraction from the filing, and the resulting arbitrage spread.

Offer-price parsing is heuristic — these prices live in free text, so we
surface a candidate and always link the filing for manual confirmation.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import logging
import os
import re
import time
import urllib.parse

import requests
import yfinance as yf

logging.getLogger("yfinance").setLevel(logging.CRITICAL)

UA = f"corp-actions-scanner {os.environ.get('SEC_CONTACT', 'corp-actions-scanner@users.noreply.github.com')}"
_session = requests.Session()
_session.headers.update({"User-Agent": UA})

# "$12.34 per share", "$12.34 net per share", "$12.34 in cash per share"
_PRICE_RE = re.compile(
    r"\$\s*([0-9]{1,5}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)\s*"
    r"(?:in\s+cash\s+|net\s+|per\s+share|cash\s+)*(?:per\s+share|a\s+share)",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")


def offer_price(url: str) -> float | None:
    """Most frequent '$X per share' figure in the primary filing document."""
    try:
        r = _session.get(url, timeout=30)
        if r.status_code != 200:
            return None
        text = _TAG_RE.sub(" ", r.text)
        prices: dict[float, int] = {}
        for m in _PRICE_RE.finditer(text):
            try:
                val = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            if 0.5 <= val <= 5000:  # filter out share counts / dollar amounts
                prices[val] = prices.get(val, 0) + 1
        time.sleep(0.15)
        if not prices:
            return None
        # Pick the price mentioned most often (ties -> highest).
        return max(prices, key=lambda p: (prices[p], p))
    except requests.RequestException:
        return None


# ---- SEC: offer price + expiration date from the "Offer to Purchase" exhibit ----
# The tender's expiration lives in exhibit (a)(1)(A) (filenames contain "a1a"),
# not in the Schedule TO cover. Amendments don't re-file it, so we fall back to
# the original SC TO-I / SC TO-T filing for that company.
_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}
_DATE = (r"(January|February|March|April|May|June|July|August|September|October|"
         r"November|December)\s+(\d{1,2})(?:\s*(?:st|nd|rd|th))?\s*,?\s+(20\d{2})")
# Offer-to-Purchase exhibit: (a)(1)(A) for third-party tenders (…a1a.htm) and
# (a)(1)(i) for issuer tenders (…a1i.htm). Match a1 + a short letter/roman tail.
_OTP_RE = re.compile(r"a1[a-z]{1,3}\.htm$", re.IGNORECASE)
_OTP_PREF = ("a1a.htm", "a1i.htm")  # the actual offer document (vs letter of transmittal etc.)
_PAR_RE = re.compile(r"par value", re.IGNORECASE)
_WD = r"(?:\w+day,?\s+)?"  # optional weekday, e.g. "Monday, June 8, 2026"
# Defined term: 'Expiration Date/Time" means July 14, 2026'.
_EXP_RE = re.compile(rf'Expiration\s+(?:Date|Time)["”\s]*(?:means|is|shall mean|will be)\s+{_WD}{_DATE}', re.IGNORECASE)
# "...will expire at one minute past 11:59 p.m., New York City time, on July 27,
# 2026" / "...EXPIRE AT 5:00 PM ... AT THE END OF JULY 24th, 2026". Anchoring on
# a time/expire cue + "on"/"end of" avoids unrelated dates (an agreement "dated
# June 13, 2024"). `.` spans the periods in "p.m." (text is whitespace-collapsed).
_EXP_RE2 = re.compile(
    rf"(?:expires?|will\s+expire|Expiration\s+(?:Date|Time)|11:59\s*[apAP]\.?\s?[mM]\.?|one\s+minute\s+(?:after|past))"
    rf".{{0,180}}?\b(?:on|at the end of)\s+{_WD}{_DATE}",
    re.IGNORECASE)
_OTP_PRICE_RE = re.compile(
    r"\$\s*([0-9]{1,4}(?:\.[0-9]{2})?)\s*(?:net[^$]{0,40}?)?per\s+[Ss]hare", re.IGNORECASE)


def _get_text(url: str) -> str | None:
    try:
        r = _session.get(url, timeout=40)
        time.sleep(0.15)
        if r.status_code != 200:
            return None
        t = html.unescape(_TAG_RE.sub(" ", r.text)).replace("\xa0", " ")
        return re.sub(r"\s+", " ", t)
    except requests.RequestException:
        return None


def _iso_date(text: str) -> str | None:
    m = re.search(_DATE, text, re.IGNORECASE)
    if not m or m.group(1).title() not in _MONTHS:
        return None
    return f"{int(m.group(3)):04d}-{_MONTHS[m.group(1).title()]:02d}-{int(m.group(2)):02d}"


# Filenames that are never the Offer to Purchase (cover, fee tables, ancillary
# letters (a)(1)(B..)/(ii..), press releases, financing (b), XBRL viewers).
_OTP_SKIP = re.compile(
    r"sctoi|sctot|filingfee|_fee|ex-?fee|ex107|ex99b|"
    r"a1(?:b|c|d|e|f|ii|iii|iv|v|vi)|transmit|guarantee|brokers|clients|"
    r"pressrelease|^r\d+\.htm", re.IGNORECASE)


def _otp_score(name: str) -> int:
    """Lower = more likely the Offer to Purchase. 9 = skip."""
    n = name.lower()
    if _OTP_SKIP.search(n):
        return 9
    if "offertopurchase" in n or re.search(r"a1a(?![a-z])", n):
        return 0
    if re.search(r"a1i(?![a-z])", n):
        return 1
    if re.search(r"a1(?![0-9a-z])", n):
        return 2
    return 4  # generic exhibit name (e.g. f45598d1.htm) — rank by size


def _otp_candidates(folder: str) -> list[str]:
    """Prioritized list of candidate Offer-to-Purchase docs in a filing folder."""
    try:
        r = _session.get(f"{folder}/index.json", timeout=30)
        time.sleep(0.1)
        items = r.json()["directory"]["item"]
    except (requests.RequestException, ValueError, KeyError):
        return []
    scored = []
    for i in items:
        if not i["name"].endswith(".htm"):
            continue
        s = _otp_score(i["name"])
        if s < 9:
            try:
                size = int(i.get("size") or 0)
            except ValueError:
                size = 0
            scored.append((s, -size, i["name"]))
    scored.sort()
    return [f"{folder}/{name}" for _, _, name in scored[:4]]


def _original_folder(cik: int, form: str) -> str | None:
    """For an amendment, locate the original SC TO-I / SC TO-T filing folder."""
    base = form.split("/")[0]
    if base not in ("SC TO-I", "SC TO-T") or not cik:
        return None
    params = {"q": "", "forms": base, "ciks": f"{cik:010d}"}
    try:
        r = _session.get(f"https://efts.sec.gov/LATEST/search-index?{urllib.parse.urlencode(params)}", timeout=30)
        time.sleep(0.15)
        hits = r.json().get("hits", {}).get("hits", [])
    except (requests.RequestException, ValueError):
        return None
    for h in sorted(hits, key=lambda x: x["_source"].get("file_date", ""), reverse=True):
        src = h["_source"]
        if src.get("form") == base:  # exact base form, not an /A amendment
            return f"https://www.sec.gov/Archives/edgar/data/{cik}/{src['adsh'].replace('-', '')}"
    return None


def _otp_price(text: str) -> float | None:
    counts: dict[float, int] = {}
    for m in _OTP_PRICE_RE.finditer(text):
        if _PAR_RE.search(text[max(0, m.start() - 20):m.start()]):
            continue  # skip "$0.01 par value per share"
        try:
            v = float(m.group(1))
        except ValueError:
            continue
        if v >= 0.10:
            counts[v] = counts.get(v, 0) + 1
    return max(counts, key=lambda p: (counts[p], p)) if counts else None


def _expiration(text: str) -> str | None:
    m = _EXP_RE.search(text) or _EXP_RE2.search(text)
    if not m:
        return None
    month, day, year = m.groups()[-3:]  # the date captured right after "on"/"means"
    if month.title() not in _MONTHS:
        return None
    return f"{int(year):04d}-{_MONTHS[month.title()]:02d}-{int(day):02d}"


def _sane_exec(exec_date: str | None, announce_date: str | None,
               max_days: int = 180, back_days: int = 14) -> str | None:
    """Keep an expiration only if it falls within a plausible window around the
    announcement — drops stale/garbage dates. A small lower bound allows the
    expiration to sit just before our announce_date (the LATEST filing), which
    happens when a final amendment reports a tender's closure. A much earlier
    date means the offer was extended (still open) — untrusted, dropped."""
    if not exec_date or not announce_date:
        return exec_date
    try:
        e = dt.date.fromisoformat(exec_date)
        a = dt.date.fromisoformat(announce_date)
    except ValueError:
        return exec_date
    return exec_date if a - dt.timedelta(days=back_days) <= e <= a + dt.timedelta(days=max_days) else None


def sec_offer_details(event: dict) -> tuple[float | None, str | None]:
    """Return (offer_price, expiration_date) for a SEC tender. Scans candidate
    Offer-to-Purchase docs (current filing, then the original for amendments)
    and keeps the first one that yields an expiration date."""
    folder = event["url"].rsplit("/", 1)[0]
    cands = _otp_candidates(folder)
    if of := _original_folder(event.get("cik"), event.get("form", "")):
        cands += _otp_candidates(of)

    price = None
    for otp in cands[:4]:
        text = _get_text(otp)
        if not text:
            continue
        price = price or _otp_price(text)
        exp = _expiration(text)
        if exp:
            return price or offer_price(event["url"]), exp
    return price or offer_price(event["url"]), None


def _price_for_symbol(symbol: str) -> float | None:
    try:
        fi = yf.Ticker(symbol).fast_info
        px = getattr(fi, "last_price", None) or getattr(fi, "previous_close", None)
        px = float(px) if px else None
        return px if (px and px == px) else None  # drop None and NaN
    except Exception:
        return None


def current_price(ticker: str | None) -> float | None:
    return _price_for_symbol(ticker) if ticker else None


# Bloomberg exchange code (from OpenFIGI) -> Yahoo Finance suffix candidates.
_EXCH_SUFFIX = {
    "GR": ".DE", "GY": ".DE", "GF": ".F", "GM": ".MU", "GS": ".SG", "GB": ".BE",
    "AV": ".VI", "SW": ".SW", "SE": ".SW", "LN": ".L", "NA": ".AS", "FP": ".PA",
    "BB": ".BR", "IM": ".MI", "SM": ".MC",
}


def ticker_from_isin(isin: str) -> tuple[str | None, float | None]:
    """Resolve an ISIN to a (Yahoo symbol, current price) via OpenFIGI, picking
    the first exchange candidate that yfinance can actually price."""
    try:
        r = _session.post(
            "https://api.openfigi.com/v3/mapping",
            json=[{"idType": "ID_ISIN", "idValue": isin}],
            timeout=20,
        )
        time.sleep(0.3)  # OpenFIGI is rate-limited without a key
        rows = (r.json() or [{}])[0].get("data", []) if r.status_code == 200 else []
    except (requests.RequestException, ValueError, IndexError):
        rows = []

    # Build ordered, de-duplicated candidate symbols from the mapping rows.
    seen, candidates = set(), []
    for row in rows:
        base = row.get("ticker")
        if not base:
            continue
        suffix = _EXCH_SUFFIX.get(row.get("exchCode", ""), "")
        for sym in ([f"{base}{suffix}"] if suffix else []) + [f"{base}.DE", f"{base}.F", base]:
            if sym not in seen:
                seen.add(sym)
                candidates.append(sym)

    for sym in candidates:
        px = _price_for_symbol(sym)
        if px:
            return sym, px
    return (candidates[0] if candidates else None), None


def enrich(event: dict) -> dict:
    """Add ticker (if ISIN-only), current_price and spread_pct in place.

    SEC offer prices are extracted here from the filing; BaFin offer prices are
    already parsed from the offer PDF by the collector.
    """
    if event.get("source") == "SEC":
        if event.get("category") in ("tender", "going_private"):
            op, exec_date = sec_offer_details(event)
            event["offer_price"] = op
            event["exec_date"] = _sane_exec(exec_date, event.get("announce_date"))
        else:  # delistings etc. — cover-page price only, no expiration
            event["offer_price"] = offer_price(event["url"])

    cp = None
    if not event.get("ticker") and event.get("isin"):
        event["ticker"], cp = ticker_from_isin(event["isin"])
    else:
        cp = current_price(event.get("ticker"))

    op = event.get("offer_price")
    event["current_price"] = cp
    if op and cp and cp > 0:
        event["spread_pct"] = round((op / cp - 1) * 100, 2)
    else:
        event["spread_pct"] = None
    return event
