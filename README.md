# Personal Finance Automation (Gmail -> LLM categorization -> Firefly III)

*[Version en espanol](README.es.md)*

An end-to-end pipeline that turns bank notification emails into a categorized,
self-hosted budget with zero manual data entry.

## Architecture

```
Bank notification email (Gmail)
        |
        v
finanzas_bpd.py  --  parses the email, extracts merchant/amount/date
        |
        +--> keyword rules (instant, free)        ~70% of transactions
        +--> Claude Haiku (batched, only for the rest)
        +--> optional Google Places enrichment (fallback for ambiguous merchants)
        |
        v
Interactive HTML dashboard (Chart.js) -- generated locally, opens in browser
        |
        v
firefly-iii/sync_all.py  --  pushes categorized transactions to a
        |                     self-hosted Firefly III instance via its REST API
        v
Firefly III (Docker: app + MariaDB + cron)  --  budgets, envelopes, reporting
```

An iOS Shortcut adds a second signal: on every Apple Pay NFC tap, it emails
itself a clean merchant name (the one Wallet shows, not the bank's messy
descriptor). The script cross-references that email against the bank
notification by amount and date (+/-1 day) and swaps in the clean name.

## Why this design

- **Layered categorization, cheapest first.** Keyword rules handle the
  majority of merchants for free and instantly; only the ambiguous ones go to
  an LLM, batched to control cost. This is the same "cheap filter before
  expensive model" pattern used in production ML pipelines.
- **Idempotent by construction.** Both the email-to-transaction cache and the
  Firefly sync track processed IDs, so re-running the pipeline never
  duplicates data.
- **No manual bookkeeping.** The whole point is that a transaction requires
  zero taps after the initial setup - the notification email is the only
  input.

## Setup

1. `pip install -r requirements.txt`
2. Google Cloud Console: enable the Gmail API, create OAuth2 credentials
   ("Desktop app"), download as `client_secret.json` in this folder.
3. Copy `config.example.json` to `config.json` and fill in your own values
   (Google Places API key is optional, only used to enrich uncategorized
   merchants).
4. Set `ANTHROPIC_API_KEY` in your environment.
5. `python finanzas_bpd.py --desde 2026-01-01 --hasta 2026-01-31`

### Firefly III (optional, for budget tracking)

`firefly-iii/` contains a `docker-compose.yml` (Firefly III + MariaDB + a cron
container for recurring jobs) and the sync scripts.

1. Copy `.env.example` -> `.env` and `.db.env.example` -> `.db.env`, filling
   in your own `APP_KEY`, `STATIC_CRON_TOKEN` (both any random 32-char
   string) and a database password (must match in both files).
2. `docker compose up -d`
3. In the Firefly III web UI (`http://localhost:8080`), create a personal
   access token, then copy `token.env.example` -> `token.env` with your
   `FIREFLY_URL` and `FIREFLY_TOKEN`.
4. Run `setup_firefly.py` once to create your categories/budgets (edit
   `PRESUPUESTOS` first with your own budget structure), then `sync_all.py`
   to push transactions.

## Files

- `finanzas_bpd.py` - the main pipeline: Gmail parsing, categorization
  (keywords + Claude), Apple Pay matching, HTML dashboard generation.
- `firefly-iii/docker-compose.yml` - self-hosted Firefly III stack.
- `firefly-iii/sync_all.py` - the main daily sync: reads Gmail via IMAP,
  classifies four notification types (consumption, ATM withdrawal, instant
  transfer, payroll deposit), and pushes them to Firefly. Includes a
  self-correcting payroll-amount estimator (the bank's email never includes
  the deposit amount, so it reuses the last confirmed one from Firefly).
- `firefly-iii/sync_firefly.py` - a simpler, single-purpose sync (one JSON
  file of transactions -> Firefly), useful as a smaller reference than
  `sync_all.py`.
- `firefly-iii/setup_firefly.py` - one-shot script to create budget
  categories/envelopes (edit `PRESUPUESTOS` with your own budget structure).
- `firefly-iii/run_sync.ps1`, `abrir_presupuesto.ps1` - Windows helpers to
  run the sync as a scheduled task and to open the dashboard.

## Skills demonstrated

Email parsing at scale, layered/cost-aware LLM usage, idempotent pipeline
design, REST API integration (Gmail, Anthropic, Google Places, Firefly III),
and self-hosting a budgeting tool with Docker.
