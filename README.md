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
- **DE-SqueezeOut** — proper German squeeze-outs (§327a AktG), delistings and
  Beherrschungs-/Gewinnabführungsverträge via the SpruchZ blog feed
  (spruchverfahren.blogspot.com), the practical proxy for the Bundesanzeiger
  (which has no API). Includes Spruchverfahren (post-deal appraisal) posts.

## Flags & alerts

- **ODD-LOT tag** — SEC tenders whose offer document contains an odd-lot
  priority provision (holders of <100 shares skip proration).
- **Fund filter** — closed-end fund / BDC repurchase tenders are flagged
  (`is_fund`) and hidden by default in the dashboard.
- **New-event alerts** — `docs/seen.json` tracks when each event was first
  seen. Genuinely new, alert-worthy events (non-fund, identifiable) open a
  GitHub issue on the repo, which triggers a normal GitHub email notification.
  Rows first seen within the last 2 days carry a 🆕 badge.

Each event carries **two dates**: `announce_date` (made public) and `exec_date`
(tender expiration / Annahmefrist Ende — the arbitrage deadline).

## Deep-dive analysis (on-demand)

Beyond the table, any single event can get a full **arbitrage memo** written by
an LLM agent that reads the offer document, checks the live quote and searches
recent news, then judges the situation (terms, spread, proration / odd-lot,
timeline, risks, verdict). This is the step that tells a real discount from a
trap the raw spread can't see (e.g. a fat spread on a board-rejected hostile
tender).

Two implementations share the same three tools (fetch document, search news,
get quote):

- `scanner/deepdive.py` — native Anthropic SDK tool-use loop.
- `scanner/deepdive_graph.py` — LangGraph version: classifies the event
  (tender / squeeze-out / merger) and routes it to a situation-specific analyst.

```bash
pip install -r requirements-deepdive.txt          # extra deps (graph version)
source scratchpad/anthropic.env                    # ANTHROPIC_API_KEY + SEC_CONTACT
DEEPDIVE_MODEL=claude-sonnet-5 \
  python -m scanner.deepdive_graph DXLG            # ticker or issuer name
```

`DEEPDIVE_MODEL` selects the model (default `claude-opus-4-8`; `claude-sonnet-5`
≈ $0.30 / memo). Each run publishes the memo as a static page in
`docs/memos/<ticker|isin>.html` and registers it in `docs/memos/manifest.json`;
the dashboard then shows a **📄 análisis** link next to that issuer. It runs
**on demand only** — deliberately **not** part of the daily cron, so it never
spends unless you ask for a specific name.

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
- **DE-Buyback:** left blank by design. The acceptance period lives only in each
  company's IR offer document (no central registry); news-scraping for it proved
  unreliable (datacenter IPs throttled/blocked, inconsistent article content).
  The offer is linked for a manual one-click check.

## Roadmap

- [x] BaFin collector (WpÜG offers, delisting-Erwerbsangebote)
- [x] SEC tender expiration-date extraction
- [x] LLM fallback extraction (Claude Haiku) for price/exec date
- [x] Squeeze-outs via SpruchZ feed (Bundesanzeiger proxy)
- [x] Odd-lot flag, fund filter, new-event alerts (GitHub issue)
- [x] Deep-dive agent memos, published to the dashboard (on-demand)
- [ ] DE-Buyback acceptance-period dates — only reliable via per-company IR
      offer-document parsing (news-scraping rejected as too fragile)
- [ ] Annualized-return column (spread / days-to-exec)
- [ ] Trigger a deep-dive from the web itself (needs a server — parked to keep
      the site 0-server; today memos are generated locally / on-demand)
