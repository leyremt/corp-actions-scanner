"""Deep-dive con LangGraph — versión experimental (prueba de concepto).

Misma idea que ``deepdive.py`` (memo de arbitraje para UNA situación) pero
montado como un GRAFO en vez de como un bucle lineal. La gracia del grafo:

    1. ``classify``  → decide qué TIPO de operación es (tender / squeeze-out /
       fusión / otro) con una heurística barata, SIN gastar LLM.
    2. según el tipo, enruta a un analista ESPECIALIZADO, cada uno con su
       propio prompt (un tender pregunta por prorrateo/odd-lot; un squeeze-out
       por Spruchverfahren; una fusión por condiciones MAC/regulatorias...).
    3. cada analista usa las MISMAS 3 herramientas de deepdive.py y escribe el
       memo.

No sustituye a deepdive.py: convive con él para poder comparar.

Uso:  python -m scanner.deepdive_graph AAG.DE
      python -m scanner.deepdive_graph "HUGO BOSS"
"""
from __future__ import annotations

import json
import os
import sys
from typing import Literal, TypedDict

from langchain_anthropic import ChatAnthropic
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import create_react_agent

# Reutilizamos la lógica ya escrita y probada de deepdive.py — no la duplicamos.
from .deepdive import _fetch_document, _find_event, _get_quote, _search_news

# Modelo elegible por variable de entorno, sin editar código:
#   DEEPDIVE_MODEL=claude-sonnet-5   → ~0,30 $/análisis (prueba barata, recomendado)
#   DEEPDIVE_MODEL=claude-haiku-4-5  → ~0,05 $/análisis (lo más barato, más flojo)
#   (sin variable)                   → claude-opus-4-8, ~1-2 $ (máxima calidad)
MODEL = os.environ.get("DEEPDIVE_MODEL", "claude-opus-4-8")

# ---------------------------------------------------------------- tools ----
# Envolvemos las funciones de deepdive.py como herramientas de LangChain.

@tool
def fetch_document(url: str) -> str:
    """Lee y devuelve el texto de un documento por URL: filing SEC (.htm),
    PDF de oferta BaFin (.pdf) o noticia. Úsalo para leer las condiciones de
    la oferta (prorrateo, umbral mínimo de aceptación, plazo)."""
    return _fetch_document(url)


@tool
def search_news(query: str) -> str:
    """Busca noticias recientes (alemán + inglés): litigios, Spruchverfahren,
    extensiones, ofertas competidoras, tasas de aceptación."""
    return _search_news(query)


@tool
def get_quote(ticker: str) -> str:
    """Precio de mercado actual y contexto para un ticker de Yahoo Finance
    (p.ej. AAG.DE, NUVL): último precio, cierre previo, media de 50 días."""
    return _get_quote(ticker)


TOOLS = [fetch_document, search_news, get_quote]


# ---------------------------------------------------------------- estado ----

class DeepDiveState(TypedDict):
    """Lo que fluye entre los nodos del grafo."""
    event: dict           # el evento del escáner (entrada)
    situation_type: str   # lo rellena classify
    memo: str             # lo rellena el analista (salida)


# ------------------------------------------------------------ nodo: clasificar
def classify(state: DeepDiveState) -> dict:
    """Decide el tipo de operación con una heurística barata (sin LLM)."""
    ev = state["event"]
    text = f"{ev.get('event_type') or ''} {ev.get('category') or ''}".lower()
    if "tender" in text or ev.get("odd_lot"):
        t = "tender"
    elif "squeeze" in text or "spruch" in text or "delisting" in text:
        t = "squeezeout"
    elif "merger" in text or "defm14a" in text or "fusion" in text or "split" in text:
        t = "merger"
    else:
        t = "other"
    return {"situation_type": t}


def route(state: DeepDiveState) -> Literal["tender", "squeezeout", "merger", "generic"]:
    """Elige a qué analista enviar según el tipo."""
    return {
        "tender": "tender",
        "squeezeout": "squeezeout",
        "merger": "merger",
    }.get(state["situation_type"], "generic")


# ------------------------------------------------------------ nodos: analistas
_BASE = """Eres un analista de situaciones especiales. Investigas UNA operación \
y escribes un memo de arbitraje conciso y accionable en español, para una \
inversora particular. Usa las herramientas para LEER el documento de oferta, \
CONSULTAR la cotización y BUSCAR noticias. Basa cada afirmación en lo que has \
leído; si un dato no consta, di "no consta". Sé escéptico con precio/spread ya \
calculados (pueden venir de extracción imperfecta): verifícalos contra el \
documento. No hagas más de 6-7 llamadas a herramientas; luego escribe el memo \
en Markdown con: Tesis, Situación, Spread y retorno, Calendario, {foco}, \
Condiciones y riesgos, Veredicto (atractivo/neutral/evitar), Fuentes."""

PROMPTS = {
    "tender": _BASE.format(
        foco="Prorrateo / odd-lot (prioridad para <100 acciones, umbral mínimo)"),
    "squeezeout": _BASE.format(
        foco="Spruchverfahren y precio de squeeze-out (posible mejora judicial)"),
    "merger": _BASE.format(
        foco="Condiciones de cierre (MAC, regulatorio, financiación, plazo)"),
    "generic": _BASE.format(foco="Puntos clave de la operación"),
}


def make_analyst(situation: str):
    """Fábrica de nodos: crea un analista con su prompt especializado."""
    agent = create_react_agent(
        ChatAnthropic(model=MODEL, max_tokens=4000),
        TOOLS,
        prompt=PROMPTS[situation],
    )

    def node(state: DeepDiveState) -> dict:
        ev = state["event"]
        seed = {k: ev.get(k) for k in (
            "source", "issuer", "bidder", "ticker", "isin", "event_type",
            "category", "announce_date", "exec_date", "offer_price",
            "current_price", "spread_pct", "odd_lot", "url", "why")}
        msg = ("Analiza esta situación y escribe el memo de arbitraje.\n\n"
               "Datos del escáner (verifícalos, pueden ser imperfectos):\n"
               + json.dumps(seed, ensure_ascii=False, indent=2)
               + f"\n\nEmpieza leyendo el documento: {ev.get('url')}")
        result = agent.invoke({"messages": [("user", msg)]})
        return {"memo": _text_of(result["messages"][-1].content)}

    return node


def _text_of(content) -> str:
    """El memo puede venir como texto plano o como lista de bloques (cuando el
    modelo razona: [{'type':'thinking',...}, {'type':'text','text':...}]).
    Nos quedamos solo con el texto."""
    if isinstance(content, str):
        return content
    parts = [b.get("text", "") for b in content
             if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(p for p in parts if p).strip()


# ------------------------------------------------------------ construir grafo
def build_app(checkpointer=None):
    g = StateGraph(DeepDiveState)
    g.add_node("classify", classify)
    for s in ("tender", "squeezeout", "merger", "generic"):
        g.add_node(s, make_analyst(s))
    g.add_edge(START, "classify")
    g.add_conditional_edges("classify", route, {
        "tender": "tender", "squeezeout": "squeezeout",
        "merger": "merger", "generic": "generic"})
    for s in ("tender", "squeezeout", "merger", "generic"):
        g.add_edge(s, END)
    # MemorySaver = checkpointing en memoria (para SQLite en disco, ver notas).
    return g.compile(checkpointer=checkpointer)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("uso: python -m scanner.deepdive_graph <ticker o nombre>")
    ev = _find_event(" ".join(sys.argv[1:]))
    if not ev:
        sys.exit(f"no encontré ningún evento para '{' '.join(sys.argv[1:])}'")
    print(f"Analizando: {ev['issuer']} ({ev.get('ticker')})…\n", file=sys.stderr)
    app = build_app(MemorySaver())
    out = app.invoke(
        {"event": ev},
        config={"configurable": {"thread_id": ev.get("ticker") or ev["issuer"]}})
    print(out["memo"])

    # Publica el memo en la web (docs/memos/) salvo que se pida --no-web
    if "--no-web" not in sys.argv:
        from .memo_web import save_memo
        fname = save_memo(ev, out["memo"], MODEL)
        print(f"\n[web] memo publicado en docs/memos/{fname}", file=sys.stderr)
