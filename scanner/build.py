"""Orchestrator: collect -> enrich -> write docs/data.json for the web page."""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

from scanner.edgar import collect
from scanner.enrich import enrich

OUT = pathlib.Path(__file__).resolve().parent.parent / "docs" / "data.json"


def main(days: int = 30) -> None:
    events = collect(days=days)
    print(f"collected {len(events)} events; enriching…", file=sys.stderr)
    for i, ev in enumerate(events, 1):
        enrich(ev)
        if i % 25 == 0:
            print(f"  {i}/{len(events)}", file=sys.stderr)

    # Arbitrage-first ordering: spread known first (widest at top),
    # then events still missing a price, newest first.
    def sort_key(e):
        has_spread = e["spread_pct"] is not None
        return (has_spread, e["spread_pct"] or -1e9, e["filed"] or "")

    events.sort(key=sort_key, reverse=True)

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "window_days": days,
        "count": len(events),
        "sources": ["SEC EDGAR"],
        "events": events,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2))
    print(f"wrote {OUT} ({len(events)} events)", file=sys.stderr)


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    main(days)
