"""Orchestrator: collect (SEC + BaFin + buybacks + squeeze-outs) -> enrich ->
track first-seen state -> write docs/data.json (+ new_events.md for alerts)."""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

from scanner import bafin, buybacks, llm_extract, squeezeouts
from scanner.edgar import collect as collect_sec
from scanner.enrich import enrich

DOCS = pathlib.Path(__file__).resolve().parent.parent / "docs"
OUT = DOCS / "data.json"
SEEN = DOCS / "seen.json"           # event id -> first_seen date (committed)
ALERT = DOCS.parent / "new_events.md"  # written only when something is alert-worthy


def _mark_new(events: list[dict]) -> list[dict]:
    """Stamp first_seen on every event; return the genuinely new ones.
    On the very first run (no seen.json) everything is baseline, not 'new'."""
    baseline = not SEEN.exists()
    seen = {} if baseline else json.loads(SEEN.read_text())
    today = dt.date.today().isoformat()
    fresh = []
    for e in events:
        first = seen.get(e["id"])
        if first is None:
            seen[e["id"]] = first = today
            if not baseline:
                fresh.append(e)
        e["first_seen"] = first
    # Prune ids gone from the feed for over a year to bound the file.
    cutoff = (dt.date.today() - dt.timedelta(days=400)).isoformat()
    current = {e["id"] for e in events}
    seen = {k: v for k, v in seen.items() if k in current or v >= cutoff}
    SEEN.write_text(json.dumps(seen, indent=0, sort_keys=True))
    return [] if baseline else fresh


def _write_alert(fresh: list[dict]) -> int:
    """Write new_events.md for the workflow to open a GitHub issue.
    Only real situations: skip fund repurchases and SEC events with no ticker."""
    worthy = [e for e in fresh
              if not e.get("is_fund") and (e.get("ticker") or e["source"] != "SEC")]
    if not worthy:
        return 0
    lines = ["Nuevas situaciones detectadas por el escáner:\n",
             "| Fuente | Ticker | Emisor | Evento | Oferta | Spread | Anuncio |",
             "|---|---|---|---|---|---|---|"]
    for e in sorted(worthy, key=lambda x: x["spread_pct"] or -1e9, reverse=True):
        cur = "$" if e["source"] == "SEC" else "€"
        offer = f"{cur}{e['offer_price']:.2f}" if e.get("offer_price") else "—"
        spread = f"{e['spread_pct']:+.1f}%" if e.get("spread_pct") is not None else "—"
        odd = " 🎯odd-lot" if e.get("odd_lot") else ""
        lines.append(f"| {e['source']} | {e.get('ticker') or '—'} | {e['issuer'][:40]}{odd} "
                     f"| {e['event_type']} | {offer} | {spread} | {e['announce_date'] or '—'} |")
    lines.append("\n[Abrir el escáner](https://leyremt.github.io/corp-actions-scanner/)")
    ALERT.write_text("\n".join(lines))
    return len(worthy)


def main(sec_days: int = 30, bafin_days: int = 365) -> None:
    events = collect_sec(days=sec_days)
    print(f"SEC: {len(events)} events", file=sys.stderr)
    for label, fn in (("BaFin", lambda: bafin.collect(days=bafin_days)),
                      ("DE-Buyback", buybacks.collect),
                      ("DE-SqueezeOut", squeezeouts.collect)):
        try:
            got = fn()
            print(f"{label}: {len(got)} events", file=sys.stderr)
            events += got
        except Exception as exc:  # never let one source kill the run
            print(f"{label} collector failed: {exc}", file=sys.stderr)

    print(f"enriching {len(events)} events…", file=sys.stderr)
    for i, ev in enumerate(events, 1):
        enrich(ev)
        if i % 25 == 0:
            print(f"  {i}/{len(events)}", file=sys.stderr)

    fresh = _mark_new(events)
    alerted = _write_alert(fresh)
    print(f"new events: {len(fresh)} (alert-worthy: {alerted})", file=sys.stderr)

    # Arbitrage-first ordering: spread known first (widest at top),
    # then events still missing a price, newest announcement first.
    def sort_key(e):
        return (e["spread_pct"] is not None, e["spread_pct"] or -1e9, e["announce_date"] or "")

    events.sort(key=sort_key, reverse=True)

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "window_days": {"SEC": sec_days, "BaFin": bafin_days},
        "count": len(events),
        "sources": ["SEC EDGAR", "BaFin", "DE-Buyback", "DE-SqueezeOut"],
        "events": events,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"LLM fallback calls: {llm_extract.calls_used()}/{llm_extract.MAX_CALLS}", file=sys.stderr)
    print(f"wrote {OUT} ({len(events)} events)", file=sys.stderr)


if __name__ == "__main__":
    sec_days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    bafin_days = int(sys.argv[2]) if len(sys.argv) > 2 else 365
    main(sec_days, bafin_days)
