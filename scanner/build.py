"""Orchestrator: collect (SEC + BaFin) -> enrich -> write docs/data.json."""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

from scanner import bafin
from scanner.edgar import collect as collect_sec
from scanner.enrich import enrich

OUT = pathlib.Path(__file__).resolve().parent.parent / "docs" / "data.json"


def main(sec_days: int = 30, bafin_days: int = 365) -> None:
    events = collect_sec(days=sec_days)
    print(f"SEC: {len(events)} events", file=sys.stderr)
    try:
        de = bafin.collect(days=bafin_days)
        print(f"BaFin: {len(de)} events", file=sys.stderr)
        events += de
    except Exception as exc:  # never let one source kill the run
        print(f"BaFin collector failed: {exc}", file=sys.stderr)

    print(f"enriching {len(events)} events…", file=sys.stderr)
    for i, ev in enumerate(events, 1):
        enrich(ev)
        if i % 25 == 0:
            print(f"  {i}/{len(events)}", file=sys.stderr)

    # Arbitrage-first ordering: spread known first (widest at top),
    # then events still missing a price, newest announcement first.
    def sort_key(e):
        return (e["spread_pct"] is not None, e["spread_pct"] or -1e9, e["announce_date"] or "")

    events.sort(key=sort_key, reverse=True)

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "window_days": {"SEC": sec_days, "BaFin": bafin_days},
        "count": len(events),
        "sources": ["SEC EDGAR", "BaFin"],
        "events": events,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"wrote {OUT} ({len(events)} events)", file=sys.stderr)


if __name__ == "__main__":
    sec_days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    bafin_days = int(sys.argv[2]) if len(sys.argv) > 2 else 365
    main(sec_days, bafin_days)
