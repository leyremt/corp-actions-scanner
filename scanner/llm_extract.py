"""LLM fallback extraction — Claude Haiku via the Anthropic API.

Regex extraction always runs first (free, deterministic). This module is
consulted only when the regex misses on a document we already downloaded:
it sends the relevant snippets to Claude Haiku with a structured-output
schema and gets back {offer_price, currency, expiration_date}.

Cost controls:
  * no-op unless ANTHROPIC_API_KEY is set (local runs without the key just
    keep today's regex-only behavior)
  * hard cap on calls per run (LLM_MAX_CALLS, default 40)
  * only the relevant windows of the document are sent (~a few K tokens),
    never the whole filing
"""
from __future__ import annotations

import json
import os
import re
import sys

MODEL = "claude-haiku-4-5"
MAX_CALLS = int(os.environ.get("LLM_MAX_CALLS", "40"))

_calls = 0
_client = None

_SCHEMA = {
    "type": "object",
    "properties": {
        "offer_price": {
            "anyOf": [{"type": "number"}, {"type": "null"}],
            "description": "Cash offer/consideration per share. null if not stated or not a cash offer.",
        },
        "currency": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "expiration_date": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "Date (YYYY-MM-DD) the acceptance/tender period ends, as currently in effect including extensions. null if not stated.",
        },
    },
    "required": ["offer_price", "currency", "expiration_date"],
    "additionalProperties": False,
}

_PROMPT = (
    "You are reading excerpts of a {hint}. Extract:\n"
    "1. offer_price: the cash price offered PER SHARE to shareholders "
    "(not par value, not prices of other agreements, not totals).\n"
    "2. expiration_date: the date the offer's acceptance/tender period ends "
    "(Annahmefrist/expiration date), as currently in effect — if the offer was "
    "extended, give the extended date. IGNORE dates of unrelated agreements "
    "(financing, cooperation, debt commitment letters).\n"
    "Use null for anything not clearly stated in the text.\n\n"
    "EXCERPTS:\n{text}"
)

_KEYWORD_RE = re.compile(
    r"expir\w+|annahmefrist|erwerbsfrist|angebotsfrist|acceptance period|"
    r"offer period|per share|je aktie|gegenleistung|barabfindung",
    re.IGNORECASE,
)


def calls_used() -> int:
    return _calls


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and _calls < MAX_CALLS


def _snippet(text: str, head: int = 4000, window: int = 700, max_windows: int = 8,
             limit: int = 20000) -> str:
    """Head of the document plus windows around period/price keywords."""
    parts = [text[:head]]
    taken = []
    for m in _KEYWORD_RE.finditer(text, head):
        start = max(head, m.start() - window // 2)
        if any(abs(start - s) < window for s in taken):
            continue
        taken.append(start)
        parts.append(text[start:start + window])
        if len(taken) >= max_windows:
            break
    return "\n[...]\n".join(parts)[:limit]


def _iso_date(value) -> str | None:
    if not isinstance(value, str):
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
    return m.group(0) if m else None


_CLASS_SCHEMA = {
    "type": "object",
    "properties": {
        "security": {
            "type": "string",
            "enum": ["equity", "debt", "other"],
            "description": "Class of security the offer is for: equity = common/"
                           "ordinary shares or stock; debt = notes, bonds, debentures.",
        },
        "is_tender_offer": {
            "type": "boolean",
            "description": "True only if securities are being acquired FROM holders "
                           "(tender / takeover / repurchase offer). False for rights "
                           "offerings and business combinations without a tender.",
        },
    },
    "required": ["security", "is_tender_offer"],
    "additionalProperties": False,
}

_CLASS_PROMPT = (
    "This is the cover text of an SEC Form CB — a foreign issuer notifying the SEC "
    "of an offer made abroad. Classify what the offer is FOR.\n"
    "Use 'debt' for notes/bonds/debentures, 'equity' for common or ordinary shares, "
    "'other' otherwise. Set is_tender_offer=false for rights offerings.\n\n"
    "TEXT:\n{text}"
)


def classify_offer(text: str) -> dict | None:
    """Classify a Form CB cover: {'security': ..., 'is_tender_offer': ...} or None.

    Only called when the free regex is ambiguous — same cost discipline as
    `extract` (shares the per-run call cap and the no-key no-op)."""
    global _calls, _client
    if not available() or not text:
        return None
    _calls += 1
    try:
        if _client is None:
            import anthropic
            _client = anthropic.Anthropic()
        resp = _client.messages.create(
            model=MODEL,
            max_tokens=100,
            output_config={"format": {"type": "json_schema", "schema": _CLASS_SCHEMA}},
            messages=[{"role": "user", "content": _CLASS_PROMPT.format(text=text[:8000])}],
        )
        if resp.stop_reason != "end_turn":
            return None
        return json.loads(resp.content[0].text)
    except Exception as exc:  # never break the pipeline
        print(f"llm classify_offer failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def extract(text: str, hint: str = "tender offer document") -> dict | None:
    """Return {offer_price, currency, expiration_date} or None on any failure."""
    global _calls, _client
    if not available() or not text:
        return None
    _calls += 1
    try:
        if _client is None:
            import anthropic
            _client = anthropic.Anthropic()
        resp = _client.messages.create(
            model=MODEL,
            max_tokens=300,
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            messages=[{
                "role": "user",
                "content": _PROMPT.format(hint=hint, text=_snippet(text)),
            }],
        )
        if resp.stop_reason != "end_turn":
            return None
        data = json.loads(resp.content[0].text)
    except Exception as exc:  # network, auth, parse — never break the pipeline
        print(f"llm_extract failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None

    price = data.get("offer_price")
    if not isinstance(price, (int, float)) or not (0.05 <= price <= 5000):
        price = None
    return {
        "offer_price": price,
        "currency": data.get("currency"),
        "expiration_date": _iso_date(data.get("expiration_date")),
    }
