"""SEC EDGAR collector.

Pulls recent corporate-action filings (tender offers, going-private,
delistings) from EDGAR full-text search and normalizes them into a common
event schema. No API key required; SEC only asks for a descriptive
User-Agent.
"""
from __future__ import annotations

import datetime as dt
import re
import time
import urllib.parse
from typing import Iterable

import requests

import os

# SEC asks for a contact email in the User-Agent. Injected via the SEC_CONTACT
# env var (a GitHub Actions secret in the cloud) so it stays out of the code.
CONTACT = os.environ.get("SEC_CONTACT", "corp-actions-scanner@users.noreply.github.com")
UA = f"corp-actions-scanner {CONTACT}"
EFTS = "https://efts.sec.gov/LATEST/search-index"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# Form types we care about -> human label + event category.
FORMS = {
    "SC TO-I": ("Issuer tender offer", "tender"),
    "SC TO-T": ("Third-party tender offer", "tender"),
    "SC 13E3": ("Going-private", "going_private"),
    "25-NSE": ("Delisting notice", "delisting"),
}

_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})


def _get(url: str, **kwargs) -> requests.Response:
    """GET with retry + SEC's 10 req/s politeness limit."""
    for attempt in range(4):
        r = _session.get(url, timeout=30, **kwargs)
        if r.status_code == 200:
            time.sleep(0.15)
            return r
        time.sleep(1.0 + attempt)
    r.raise_for_status()
    return r


def cik_to_ticker() -> dict[int, str]:
    """Map CIK -> primary ticker from SEC's published list."""
    data = _get(TICKERS_URL).json()
    out: dict[int, str] = {}
    for row in data.values():
        out.setdefault(int(row["cik_str"]), row["ticker"])
    return out


def _search(form: str, start: str, end: str) -> list[dict]:
    params = {
        "q": "",
        "forms": form,
        "dateRange": "custom",
        "startdt": start,
        "enddt": end,
    }
    url = f"{EFTS}?{urllib.parse.urlencode(params)}"
    return _get(url).json().get("hits", {}).get("hits", [])


def collect(days: int = 30) -> list[dict]:
    """Return normalized filing events from the last `days` days."""
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    tickers = cik_to_ticker()
    events: dict[str, dict] = {}

    for form, (label, category) in FORMS.items():
        for hit in _search(form, start.isoformat(), end.isoformat()):
            src = hit.get("_source", {})
            adsh = src.get("adsh")
            if not adsh:
                continue
            cik = int(src["ciks"][0]) if src.get("ciks") else None
            accession_id, _, filename = hit.get("_id", "").partition(":")
            url = (
                f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                f"{adsh.replace('-', '')}/{filename}"
                if cik and filename
                else f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
            )
            raw_name = (src.get("display_names") or ["?"])[0]
            name = re.sub(r"\s*\([^)]*\)\s*$", "", raw_name.split("  (CIK")[0]).strip()
            # Skip amendments by keeping only the latest filing per issuer+category.
            key = f"{cik}:{category}"
            ev = {
                "id": hit.get("_id"),
                "source": "SEC",
                "issuer": name,
                "bidder": None,
                "cik": cik,
                "ticker": tickers.get(cik) if cik else None,
                "isin": None,
                "form": src.get("form", form),
                "event_type": label,
                "category": category,
                "announce_date": src.get("file_date"),  # SEC filing = announcement
                "exec_date": None,  # tender expiration not parsed yet (SEC)
                "url": url,
            }
            prev = events.get(key)
            if prev is None or (ev["announce_date"] or "") >= (prev["announce_date"] or ""):
                events[key] = ev

    return sorted(events.values(), key=lambda e: e["announce_date"] or "", reverse=True)


if __name__ == "__main__":
    import json

    rows = collect(days=30)
    print(f"{len(rows)} events")
    print(json.dumps(rows[:5], indent=2))
