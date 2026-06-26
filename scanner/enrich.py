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


def current_price(ticker: str | None) -> float | None:
    if not ticker:
        return None
    try:
        fi = yf.Ticker(ticker).fast_info
        px = getattr(fi, "last_price", None) or getattr(fi, "previous_close", None)
        return float(px) if px else None
    except Exception:
        return None


def enrich(event: dict) -> dict:
    """Add offer_price, current_price and spread_pct to an event in place."""
    op = offer_price(event["url"])
    cp = current_price(event.get("ticker"))
    event["offer_price"] = op
    event["current_price"] = cp
    if op and cp and cp > 0:
        event["spread_pct"] = round((op / cp - 1) * 100, 2)
    else:
        event["spread_pct"] = None
    return event
