"""BaFin collector — German takeover / delisting offers under the WpÜG.

Two structured BaFin databases drive this:
  * Angebotsankündigung  — offer announcements (target, ISIN, type, announce date)
  * Angebotsunterlagen    — published offer documents (link to the offer PDF)

The announcement date is taken from the first table; the offer price and the
acceptance-period end date (Annahmefrist Ende = our "execution" date) are parsed
off the standardized cover page of the offer PDF, which BaFin formats
consistently, e.g.:

    "... Barabfindung in Höhe von EUR 38,00 je Aktie ...
     Annahmefrist: 25. Juni 2026 bis 27. Juli 2026, 24:00 Uhr"

Squeeze-outs proper (aktien-/verschmelzungsrechtlich) are NOT here — they run
through the Bundesanzeiger / shareholder meetings, a later phase.
"""
from __future__ import annotations

import datetime as dt
import html
import io
import re
import time

import pypdf
import requests

from scanner import llm_extract

import os

UA = f"Mozilla/5.0 (corp-actions-scanner; {os.environ.get('SEC_CONTACT', 'corp-actions-scanner@users.noreply.github.com')})"
BASE = "https://www.bafin.de/DE/die-bafin/publikationen-daten/datenbanken-uebersichten/WPUeG"
ANNOUNCE_URL = f"{BASE}/angebotsankuendigung/li-angebotsankuendigung-wpueg_node.html"
DOCS_URL = f"{BASE}/angebotsunterlagen/angebotsunterlagen_node.html"

# "Inhalt der Veröffentlichung" -> (English label, our category)
TYPE_MAP = {
    "Übernahmeangebot": ("Takeover offer", "tender"),
    "Pflichtangebot": ("Mandatory offer", "tender"),
    "Erwerbsangebot": ("Acquisition offer", "tender"),
    "Delisting-Erwerbsangebot": ("Delisting offer", "delisting"),
    "Delisting": ("Delisting offer", "delisting"),
}
_MONTHS = {
    "januar": 1, "februar": 2, "märz": 3, "april": 4, "mai": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11,
    "dezember": 12,
}

_session = requests.Session()
_session.headers.update({"User-Agent": UA})

_TAG_RE = re.compile(r"<[^>]+>")
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
_HREF_RE = re.compile(r'href="([^"]+)"')


def _clean(raw: str) -> str:
    txt = html.unescape(re.sub(r"\s+", " ", _TAG_RE.sub(" ", raw))).strip()
    return re.sub(r"\s+([,;])", r"\1", txt)  # "AG , Köln" -> "AG, Köln"


def _get(url: str) -> requests.Response:
    for attempt in range(3):
        r = _session.get(url, timeout=40)
        if r.status_code == 200:
            time.sleep(0.2)
            return r
        time.sleep(1.0 + attempt)
    r.raise_for_status()
    return r


def _de_date(s: str) -> str | None:
    """'27. Juli 2026' or '27.07.2026' -> ISO date string."""
    s = s.strip()
    m = re.match(r"(\d{1,2})\.\s*([A-Za-zäöüÄÖÜ]+)\s*(\d{4})", s)
    if m and m.group(2).lower() in _MONTHS:
        return f"{int(m.group(3)):04d}-{_MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return None


def _de_num(s: str) -> float | None:
    """German number '1.234,56' -> 1234.56."""
    try:
        return float(s.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def _rows(html: str) -> list[list[str]]:
    out = []
    for r in _ROW_RE.findall(html):
        cells = [_clean(c) for c in _CELL_RE.findall(r)]
        if cells:
            out.append((cells, r))
    return out


def _parse_announcements() -> dict[str, dict]:
    """ISIN -> announcement record (latest)."""
    html = _get(ANNOUNCE_URL).text
    out: dict[str, dict] = {}
    for cells, _ in _rows(html):
        if len(cells) < 5 or cells[0].startswith("Bieter"):
            continue
        bidder, target, isin, content, pub = cells[0], cells[1], cells[2].replace(" ", ""), cells[3], cells[4]
        if not re.match(r"^[A-Z]{2}[A-Z0-9]{9}\d$", isin):
            continue
        label_cat = next((v for k, v in TYPE_MAP.items() if k in content), (content, "tender"))
        rec = {
            "bidder": bidder, "target": target, "isin": isin,
            "event_type": label_cat[0], "category": label_cat[1],
            "announce_date": _de_date(pub),
        }
        prev = out.get(isin)
        if prev is None or (rec["announce_date"] or "") >= (prev["announce_date"] or ""):
            out[isin] = rec
    return out


def _parse_doc_links() -> dict[str, str]:
    """ISIN -> offer-document landing-page URL."""
    html = _get(DOCS_URL).text
    out: dict[str, str] = {}
    for cells, raw in _rows(html):
        if len(cells) < 3:
            continue
        isin = cells[2].replace(" ", "")
        if not re.match(r"^[A-Z]{2}[A-Z0-9]{9}\d$", isin):
            continue
        href = next((h for h in _HREF_RE.findall(raw) if "Angebotsunterlage" in h), None)
        if href:
            out.setdefault(isin, href if href.startswith("http") else "https://www.bafin.de" + href)
    return out


def _pdf_url_from_landing(landing: str) -> str | None:
    html = _get(landing).text
    m = next((h for h in _HREF_RE.findall(html) if h.lower().endswith(".pdf") or ".pdf?" in h.lower()), None)
    if not m:
        return None
    return m if m.startswith("http") else "https://www.bafin.de" + m


def _parse_offer_pdf(pdf_url: str) -> tuple[float | None, str | None]:
    """Return (offer_price, acceptance-period-end ISO date) from the cover page."""
    try:
        data = _get(pdf_url).content
        reader = pypdf.PdfReader(io.BytesIO(data))
        txt = re.sub(r"[ \t]+", " ", "\n".join((p.extract_text() or "") for p in reader.pages[:6]))
    except Exception:
        return None, None
    flat = txt.replace("\n", " ")
    price = None
    pm = re.search(r"(?:EUR|€)\s*([0-9.]+,\d{2})\s*(?:je|pro)\s*(?:Aktie|Stückaktie)", flat)
    if pm:
        price = _de_num(pm.group(1))
    end = None
    am = re.search(r"Annahmefrist[:\s]*([0-9]{1,2}\.\s*\w+\s*[0-9]{4})\s*bis\s*([0-9]{1,2}\.\s*\w+\s*[0-9]{4})", flat)
    if am:
        end = _de_date(am.group(2))
    # Regex missed something — LLM fallback on the same PDF text.
    if (price is None or end is None) and llm_extract.available():
        res = llm_extract.extract(flat, hint="German WpÜG Angebotsunterlage (takeover offer document)")
        if res:
            price = price if price is not None else res["offer_price"]
            end = end or res["expiration_date"]
    return price, end


def collect(days: int = 180, max_pdfs: int = 60) -> list[dict]:
    """Normalized BaFin offer events from the last `days` days (by announce date)."""
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    announcements = _parse_announcements()
    doc_links = _parse_doc_links()

    recent = sorted(
        (a for a in announcements.values() if (a["announce_date"] or "") >= cutoff),
        key=lambda a: a["announce_date"] or "", reverse=True,
    )

    events, pdfs_done = [], 0
    for a in recent:
        isin = a["isin"]
        offer_price = exec_date = None
        landing = doc_links.get(isin)
        url = landing or DOCS_URL
        if landing and pdfs_done < max_pdfs:
            pdf = _pdf_url_from_landing(landing)
            if pdf:
                offer_price, exec_date = _parse_offer_pdf(pdf)
                # Sanity: acceptance period must end after the announcement.
                if exec_date and a["announce_date"] and exec_date < a["announce_date"]:
                    exec_date = None
                url = pdf
                pdfs_done += 1
        events.append({
            "id": f"bafin:{isin}:{a['announce_date']}",
            "source": "BaFin",
            "issuer": a["target"],
            "bidder": a["bidder"],
            "cik": None,
            "ticker": None,            # resolved from ISIN during enrichment
            "isin": isin,
            "form": a["event_type"],
            "event_type": a["event_type"],
            "category": a["category"],
            "announce_date": a["announce_date"],
            "exec_date": exec_date,
            "offer_price": offer_price,
            "url": url,
        })
    return events


if __name__ == "__main__":
    import json
    rows = collect(days=180, max_pdfs=12)
    print(f"{len(rows)} BaFin events")
    print(json.dumps(rows[:6], indent=2, ensure_ascii=False))
