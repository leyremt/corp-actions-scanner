"""Enrichment: current market price (yfinance) + best-effort offer-price
extraction from the filing, and the resulting arbitrage spread.

Offer-price parsing is heuristic — these prices live in free text, so we
surface a candidate and always link the filing for manual confirmation.
"""
from __future__ import annotations

import logging
import re
import time

import requests
import yfinance as yf

logging.getLogger("yfinance").setLevel(logging.CRITICAL)

UA = "corp-actions-scanner corp-actions-scanner@users.noreply.github.com"
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
