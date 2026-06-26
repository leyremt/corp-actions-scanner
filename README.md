# Corporate Actions Scanner

Watches **SEC EDGAR** for special-situation corporate actions — odd-lot/tender
offers, going-private deals and delistings — computes the **arbitrage spread**
(offer price vs. current market price) and publishes a sortable web page.

Runs itself daily in the cloud via GitHub Actions; no server, no paid data feed.

## How it works

```
GitHub Actions (cron)
   └─ python -m scanner.build 30
        ├─ scanner/edgar.py   pull SC TO-I / SC TO-T / SC 13E3 / 25-NSE filings
        ├─ scanner/enrich.py  current price (yfinance) + offer price (heuristic)
        └─ docs/data.json     written + committed
   └─ GitHub Pages serves docs/index.html → the dashboard
```

## Local run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m scanner.build 30        # window in days
open docs/index.html              # data.json is served alongside
```

## Known limits (MVP)

- **Offer-price extraction is heuristic.** It regexes `$X per share` out of the
  filing text. Large spreads (>25%) are usually parsing errors, not real edge —
  always confirm against the linked filing. This is the first thing to harden.
- **Coverage is SEC-only.** German names (Bundesanzeiger / BaFin squeeze-outs &
  delistings — CLIQ/Aumann territory) are Phase 2.
- Closed-end funds / BDCs file many routine repurchase tenders; they have no
  ticker mapping and sink to the bottom (no spread).

## Roadmap

- [ ] Harden offer-price extraction (per-form parsers, confidence flag)
- [ ] BaFin collector (WpÜG offers, delisting-Erwerbsangebote)
- [ ] Bundesanzeiger collector (squeeze-outs, HV convocations)
- [ ] Email/Telegram digest of new wide-spread events
