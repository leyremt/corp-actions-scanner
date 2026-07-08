"""Publica un memo de deep-dive como página estática dentro de docs/.

Encaja con la arquitectura "0 servidor": convierte el memo (Markdown) a una
página HTML autocontenida en ``docs/memos/<clave>.html`` y mantiene un índice
``docs/memos/manifest.json`` que la web (index.html) lee para mostrar el enlace
"📄 análisis" en la fila correspondiente.

La clave de cada memo es el ticker (o el ISIN si no hay ticker), igual que usa
la web para identificar cada fila.
"""
from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime, timezone

_DOCS = os.path.join(os.path.dirname(__file__), "..", "docs")
_MEMOS_DIR = os.path.join(_DOCS, "memos")
_MANIFEST = os.path.join(_MEMOS_DIR, "manifest.json")


# --------------------------------------------------------- Markdown → HTML ---
# Conversor mínimo para el subconjunto que usan los memos: títulos, negrita,
# cursiva, enlaces, listas, tablas y separadores. Sin dependencias externas.

def _inline(t: str) -> str:
    t = html.escape(t)
    t = re.sub(r"\[(.+?)\]\((.+?)\)",
               r'<a href="\2" target="_blank" rel="noopener">\1</a>', t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", t)
    return t


def md_to_html(md: str) -> str:
    lines = md.split("\n")
    out, i, n = [], 0, len(md.split("\n"))
    while i < n:
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        if re.fullmatch(r"-{3,}", s):
            out.append("<hr>"); i += 1; continue
        m = re.match(r"(#{1,4})\s+(.*)", s)
        if m:
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_inline(m.group(2))}</h{lvl}>"); i += 1; continue
        # tabla: fila con | seguida de una fila separadora tipo |---|---|
        if s.startswith("|") and i + 1 < n:
            sep = lines[i + 1].strip()
            if "-" in sep and set(sep) <= set("|-: "):
                header = [c.strip() for c in s.strip("|").split("|")]
                i += 2
                body = []
                while i < n and lines[i].strip().startswith("|"):
                    body.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                    i += 1
                th = "".join(f"<th>{_inline(c)}</th>" for c in header)
                trs = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>"
                              for r in body)
                out.append(f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>")
                continue
        # listas
        if re.match(r"[-*]\s+", s) or re.match(r"\d+\.\s+", s):
            ordered = bool(re.match(r"\d+\.\s+", s))
            items = []
            while i < n and (re.match(r"[-*]\s+", lines[i].strip())
                             or re.match(r"\d+\.\s+", lines[i].strip())):
                items.append("<li>" + _inline(re.sub(r"^([-*]|\d+\.)\s+", "", lines[i].strip())) + "</li>")
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue
        # párrafo
        para = [s]
        i += 1
        while i < n:
            nx = lines[i].strip()
            if (not nx or re.match(r"#{1,4}\s", nx) or nx.startswith("|")
                    or re.match(r"[-*]\s+", nx) or re.fullmatch(r"-{3,}", nx)):
                break
            para.append(nx); i += 1
        out.append(f"<p>{_inline(' '.join(para))}</p>")
    return "\n".join(out)


# ------------------------------------------------------------- página HTML ---
_PAGE = """<!doctype html>
<html lang="es"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ · Análisis</title>
<style>
 :root{--bg:#0d1117;--panel:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;--accent:#58a6ff}
 *{box-sizing:border-box}
 body{margin:0;font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;background:var(--bg);color:var(--text)}
 .top{padding:16px 24px;border-bottom:1px solid var(--border)}
 .top a{color:var(--accent);text-decoration:none;font-size:13px}
 .top a:hover{text-decoration:underline}
 main{max-width:820px;margin:0 auto;padding:28px 24px 60px}
 h1{font-size:24px;margin:.2em 0 .4em} h2{font-size:18px;margin:1.4em 0 .4em;border-bottom:1px solid var(--border);padding-bottom:.25em}
 h3{font-size:15px;margin:1.2em 0 .3em}
 p{margin:.6em 0} strong{color:#fff} a{color:var(--accent)}
 ul,ol{margin:.5em 0 .8em 1.2em} li{margin:.25em 0}
 hr{border:0;border-top:1px solid var(--border);margin:1.6em 0}
 table{border-collapse:collapse;width:100%;margin:1em 0;font-size:14px}
 th,td{border:1px solid var(--border);padding:7px 10px;text-align:left}
 th{background:var(--panel);color:var(--muted)}
 .disc{margin-top:40px;padding:12px 14px;background:var(--panel);border:1px solid var(--border);border-radius:6px;color:var(--muted);font-size:12px}
</style></head><body>
<div class="top"><a href="../index.html">← Volver al escáner</a></div>
<main>
__BODY__
<div class="disc">Análisis generado automáticamente por un agente (modelo: __MODEL__) el __DATE__.
Es una herramienta de apoyo, no una recomendación de inversión; verifica siempre contra el documento original.</div>
</main></body></html>
"""


def _safe(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", key)


def save_memo(event: dict, memo_md: str, model: str) -> str:
    """Guarda el memo como página en docs/memos/ y actualiza el manifest.
    Devuelve la ruta relativa del archivo creado (p. ej. 'CEC.DE.html')."""
    key = event.get("ticker") or event.get("isin") or event.get("issuer") or "memo"
    fname = f"{_safe(key)}.html"
    os.makedirs(_MEMOS_DIR, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    page = (_PAGE.replace("__TITLE__", html.escape(event.get("issuer") or key))
                 .replace("__BODY__", md_to_html(memo_md))
                 .replace("__MODEL__", html.escape(model))
                 .replace("__DATE__", now))
    with open(os.path.join(_MEMOS_DIR, fname), "w", encoding="utf-8") as f:
        f.write(page)

    manifest = {}
    if os.path.exists(_MANIFEST):
        try:
            manifest = json.load(open(_MANIFEST, encoding="utf-8"))
        except Exception:
            manifest = {}
    manifest[key] = {
        "file": fname,
        "issuer": event.get("issuer"),
        "ticker": event.get("ticker"),
        "model": model,
        "generated_at": now,
    }
    with open(_MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return fname
