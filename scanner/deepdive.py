"""On-demand deep-dive agent — an arbitrage memo for a single situation.

This is the one genuinely *agentic* piece of the project: given one event, an
Anthropic tool-use loop decides what to read (offer document, filings, news),
pulls the data via three tools, and writes a structured memo (terms, spread,
proration / odd-lot, timeline, risks, thesis). Uses the Anthropic SDK's own
tool loop — no LangChain — since the whole project is already Anthropic and a
single research agent doesn't need a graph framework.

Run:  python -m scanner.deepdive AAG.DE
      python -m scanner.deepdive "HUGO BOSS"
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET

import requests

MODEL = "claude-opus-4-8"
MAX_STEPS = 8
DATA = os.path.join(os.path.dirname(__file__), "..", "docs", "data.json")
UA = f"Mozilla/5.0 (corp-actions-scanner; {os.environ.get('SEC_CONTACT', 'corp-actions-scanner@users.noreply.github.com')})"
_session = requests.Session()
_session.headers.update({"User-Agent": UA})

# ---------------------------------------------------------------- tools ----

TOOLS = [
    {
        "name": "fetch_document",
        "description": "Fetch and read a document by URL — an SEC filing (.htm), "
                       "a BaFin offer PDF (.pdf), or a news article. Returns the "
                       "extracted text (truncated). Use this to read the offer terms, "
                       "acceptance/proration conditions, minimum-tender thresholds, etc.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "search_news",
        "description": "Search recent news (German + English) for a query — use it to "
                       "check for litigation, Spruchverfahren (appraisal proceedings), "
                       "deal updates, extensions, competing bids, or acceptance-rate news. "
                       "Returns recent headlines with snippets and links.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "get_quote",
        "description": "Current market price and recent context for a Yahoo Finance "
                       "ticker (e.g. AAG.DE, NUVL). Returns last price, previous close, "
                       "and 50-day average.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
]


def _clean(html_txt: str, limit: int = 14000) -> str:
    t = re.sub(r"<[^>]+>", " ", html_txt)
    import html as _html
    t = re.sub(r"\s+", " ", _html.unescape(t)).strip()
    return t[:limit]


def _fetch_document(url: str) -> str:
    try:
        r = _session.get(url, timeout=40)
        if r.status_code != 200:
            return f"[HTTP {r.status_code} fetching {url}]"
        ctype = r.headers.get("content-type", "")
        if "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(r.content))
            txt = "\n".join((p.extract_text() or "") for p in reader.pages[:15])
            return _clean(txt)
        return _clean(r.text)
    except Exception as exc:
        return f"[error fetching {url}: {type(exc).__name__}]"


def _search_news(query: str) -> str:
    q = urllib.parse.quote(query)
    out = []
    for url in (f"https://www.bing.com/news/search?q={q}&format=rss&setlang=de&cc=DE",
                f"https://news.google.com/rss/search?q={q}&hl=de&gl=DE&ceid=DE:de"):
        try:
            items = ET.fromstring(_session.get(url, timeout=25).content).findall(".//item")
        except Exception:
            continue
        for it in items[:6]:
            title = (it.findtext("title") or "").strip()
            desc = re.sub(r"<[^>]+>", " ", it.findtext("description") or "")[:180]
            link = it.findtext("link") or ""
            out.append(f"- {title}\n  {desc.strip()}\n  {link}")
        if out:
            break
    return "\n".join(out[:8]) or "[no recent news found]"


def _get_quote(ticker: str) -> str:
    try:
        import yfinance as yf
        fi = yf.Ticker(ticker).fast_info
        last = getattr(fi, "last_price", None)
        prev = getattr(fi, "previous_close", None)
        avg = getattr(fi, "fifty_day_average", None)
        cur = getattr(fi, "currency", "")
        return (f"{ticker}: last={last} prev_close={prev} 50d_avg={avg} currency={cur}")
    except Exception as exc:
        return f"[quote error for {ticker}: {type(exc).__name__}]"


def _run_tool(name: str, inp: dict) -> str:
    if name == "fetch_document":
        return _fetch_document(inp.get("url", ""))
    if name == "search_news":
        return _search_news(inp.get("query", ""))
    if name == "get_quote":
        return _get_quote(inp.get("ticker", ""))
    return f"[unknown tool {name}]"


# --------------------------------------------------------------- agent ----

_SYSTEM = """Eres un analista de situaciones especiales (arbitraje de fusiones, \
tender offers, squeeze-outs, delistings). Investigas UNA operación y escribes un \
memo de arbitraje conciso y accionable en español, para una inversora particular.

Trabaja así:
- Usa las herramientas para LEER el documento de oferta (condiciones, prorrateo, \
umbral mínimo de aceptación, plazo), CONSULTAR la cotización actual y BUSCAR \
noticias (litigios, Spruchverfahren, extensiones, ofertas competidoras).
- Basa cada afirmación en lo que has leído. Si un dato no consta, di "no consta" \
— no lo inventes.
- Sé escéptico con los datos ya calculados (precio/spread pueden venir de \
extracción imperfecta): verifícalos contra el documento.

Cuando tengas suficiente, responde SOLO con el memo en Markdown, con estas \
secciones (omite una sección si de verdad no hay nada que decir):

## <Empresa> — <tipo de operación>
**Tesis (2-3 líneas):** …
**Situación:** qué, quién (bidder), precio de oferta, forma (efectivo/canje).
**Spread y retorno:** spread actual vs. cotización; retorno anualizado hasta el plazo si hay fecha.
**Prorrateo / odd-lot:** si la oferta es parcial o hay prioridad odd-lot, explica el escenario para <100 acciones.
**Calendario:** fechas clave (anuncio, plazo de aceptación, cierre esperado).
**Condiciones y riesgos:** umbral mínimo, condiciones regulatorias/MAC, financiación, litigios/Spruchverfahren.
**Veredicto:** atractivo / neutral / evitar, con una razón.
**Fuentes:** los documentos/enlaces que has usado.

No hagas más de 6-7 llamadas a herramientas; luego escribe el memo."""


def deepdive(event: dict, verbose: bool = True) -> str:
    import anthropic
    client = anthropic.Anthropic()

    seed = {k: event.get(k) for k in (
        "source", "issuer", "bidder", "ticker", "isin", "event_type", "category",
        "announce_date", "exec_date", "offer_price", "current_price", "spread_pct",
        "odd_lot", "url", "why")}
    messages = [{
        "role": "user",
        "content": "Analiza esta situación y escribe el memo de arbitraje.\n\n"
                   "Datos que ya tiene el escáner (verifícalos, pueden ser imperfectos):\n"
                   + json.dumps(seed, ensure_ascii=False, indent=2)
                   + f"\n\nEmpieza leyendo el documento de la oferta: {event.get('url')}",
    }]

    for step in range(MAX_STEPS):
        resp = client.messages.create(
            model=MODEL, max_tokens=4000, system=_SYSTEM, tools=TOOLS, messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text").strip()

        results = []
        for b in resp.content:
            if b.type == "tool_use":
                if verbose:
                    arg = b.input.get("url") or b.input.get("query") or b.input.get("ticker")
                    print(f"  · {b.name}({str(arg)[:70]})", file=sys.stderr)
                results.append({
                    "type": "tool_result", "tool_use_id": b.id,
                    "content": _run_tool(b.name, b.input),
                })
        messages.append({"role": "user", "content": results})
    return "[el agente no terminó dentro del límite de pasos]"


def _find_event(query: str) -> dict | None:
    d = json.load(open(DATA, encoding="utf-8"))
    q = query.lower()
    for e in d["events"]:
        if (e.get("ticker") or "").lower() == q:
            return e
    for e in d["events"]:
        if q in (e.get("issuer") or "").lower() or q in (e.get("ticker") or "").lower():
            return e
    return None


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("uso: python -m scanner.deepdive <ticker o nombre>")
    ev = _find_event(" ".join(sys.argv[1:]))
    if not ev:
        sys.exit(f"no encontré ningún evento para '{' '.join(sys.argv[1:])}'")
    print(f"Analizando: {ev['issuer']} ({ev.get('ticker')})…\n", file=sys.stderr)
    print(deepdive(ev))
