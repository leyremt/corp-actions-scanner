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

## Sources

- **SEC EDGAR** — SC TO-I / SC TO-T / SC 13E3 / 25-NSE. Announce date = filing
  date. Offer price = heuristic regex over the filing text.
- **BaFin (WpÜG)** — German takeover / mandatory / delisting offers from the
  Angebotsankündigung + Angebotsunterlagen databases. Announce date is
  structured; offer price and acceptance-period end (`exec_date`) are parsed
  off the standardized offer-PDF cover page. ISIN → ticker via OpenFIGI.
- **DE-Buyback** — German issuer share-buyback / self-tender offers
  (Aktienrückkaufangebote, e.g. Aumann). These are NOT WpÜG offers and have no
  central registry, so they are sourced from Google News RSS filtered on the
  standard German terms; company name → Yahoo symbol for a live price. Lower
  precision (news-derived); the row links the source article.

Each event carries **two dates**: `announce_date` (made public) and `exec_date`
(tender expiration / Annahmefrist Ende — the arbitrage deadline).

## Known limits

- **SEC offer-price extraction is heuristic.** Large spreads (>25%) are usually
  parsing errors — always confirm against the linked filing.
- **BaFin is high-signal but sparse** (~12 offers/year), so it uses a 365-day
  window. Freshly announced offers have no document yet → price/exec pending.
- **Squeeze-outs proper** (aktien-/verschmelzungsrechtlich) are not in BaFin —
  they run through the Bundesanzeiger (Phase 3).

## Execution date

- **SEC tenders (SC TO-I / SC TO-T):** the expiration is parsed from the
  Offer-to-Purchase exhibit ((a)(1)(A)); for amendments we fall back to the
  original filing. Kept only if it lands within ~180 days of the announcement.
- **SEC mergers (SC 13E3):** no fixed expiration (they close on vote/conditions)
  → left blank by design.
- **BaFin:** acceptance-period end (Annahmefrist).
- **DE-Buyback:** not yet — the acceptance period is in the offer document on the
  company's IR site, not in the news headline.

## Roadmap

- [x] BaFin collector (WpÜG offers, delisting-Erwerbsangebote)
- [x] SEC tender expiration-date extraction
- [ ] DE-Buyback acceptance-period dates (parse the IR offer document)
- [ ] Bundesanzeiger collector (squeeze-outs, HV convocations)
- [ ] Email/Telegram digest of new wide-spread events
