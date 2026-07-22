# 07 — Roadmap

## Phase 0 — Specification *(this, done 2026-07-21)*

Design docs in `docs/`. Exit criterion: operator sign-off on the specs.

## Phase 1 — Walking skeleton (US EOD MVP)

The smallest thing a student could actually use.

- Flat Terraform root (`data` + `control` tables and the admin allowlist) plus
  `serverless.yml` (HTTP API, authorizer, API Lambda).
- Serverless Framework v4.39.0 selected and pinned; its one-time account login is
  an operator deployment prerequisite (see [06-infrastructure](06-infrastructure.md)).
- Auth path end-to-end: one-time bootstrap script → admin key; seeded student key
  → authorizer → quota counter → `429`.
- Yahoo chart adapter + `GET /v1/candles/{symbol}` + `GET /v1/symbols/{symbol}` for a
  ~20-symbol dev universe, manually backfilled 10 years.
- `GET /v1/health` backed by market-status items; `GET /v1/usage` keyed by the
  authorizer's non-secret `keyId`.
- Source acceptance spike before the full build: the Stooq CSV path returned a
  browser proof-of-work page in July 2026, so Phase 1 does not automate around
  it. Prove the Yahoo chart path returns ten years of AAPL data and capture its
  response shape in adapter tests.
- `docs/openapi.yaml` started; becomes the contract of record.

**Exit criterion:** a curl with a dev key returns 10 years of real AAPL candles.

## Phase 2 — Ingestion machinery *(implemented 2026-07-21; initial backfill completed 2026-07-22; schedule observation pending)*

- US EventBridge schedule → dispatcher → SQS → worker; DLQ + alarm.
- Backfill jobs (`JOB#` tracking, symbol-year messages, resumability).
- Alpha Vantage adapter, fallback chains, source budgets, circuit breakers.
- Versioned 515-symbol US universe (503 S&P 500 securities + 12 ETFs); its
  initial 5,665-item historical backfill completed successfully.

**Exit criterion:** nightly cron keeps the US universe current with zero manual
steps for a week. This requires post-deployment observation and is not claimed by
the code-complete status above.

## Phase 3 — Full coverage + quotes + synthetic

- NSE/BSE (Yahoo), FX (frankfurter), crypto recent data (CoinGecko), and crypto
  deep history (Yahoo) universes + schedules.
- Corporate actions: `ADJ` item ingestion (Yahoo events, weekly sweep for
  Stooq-sourced symbols) + read-time `adjclose` in candle responses, with tests
  pinned against Yahoo's published `Adj Close` for a handful of split/dividend
  symbols (AAPL 2020 4:1, NVDA 2024 10:1, a steady dividend payer).
- `GET /v1/quotes/{symbol}` + batch endpoint, Finnhub adapter, quote cache/TTL.
- Synthetic generator with determinism tests (same stored anchor + seed → same candles).
- On-demand symbol discovery + lazy backfill (`202` flow).

**Exit criterion:** kill all upstream access in dev → registered symbols with
stored history still serve candles and quotes using stored, stale, or explicitly
synthetic data. A newly discovered symbol without a real anchor may remain `202`.

## Phase 4 — Cohort operations & polish

- Admin API + `fnx` CLI; cohort/key lifecycle; CSV issuance flow.
- Student-facing docs: quickstart (curl/Python/JS/Excel), error-handling guide,
  fair-use page. This is also capstone collateral — students read these docs as
  an example of what API documentation should look like.
- Observability pass: dashboard, alarm runbook, budget alert.
- Dry run with a pilot group before the first real cohort.

**Exit criterion:** issue 50 keys for a real cohort in < 10 minutes, survive week one without operator intervention.

## Backlog (v1.x, unscheduled)

- `1wk`/`1mo` computed intervals; CSV format on candles.
- Optional custom domain and Route 53 mapping (raw API Gateway URL is v1 default).
- NSE bhavcopy official-archive adapter (reduce Yahoo dependence for India).
- Standalone corporate-actions endpoint (`GET /v1/actions/{symbol}`) exposing
  the raw split/dividend events already stored in `ADJ` items.
- Company fundamentals (profile, ratios) — most-requested v2 data type.
- GitHub Actions plan/apply pipeline.

## v2 — Simulation module (deliberately deferred)

Reserved, not designed. Constraints already locked so v1 won't fight it:

- URL space `/v1/sim/*` reserved (routers must not claim it).
- Paper-trading accounts, orders filled against Fauxnance quotes, positions,
  P&L — a backend for weaker capstone teams to lean on.
- Likely additions: a `sim` table, DynamoDB Streams for mark-to-market, and
  per-key linkage from control-table keys to sim accounts (why `keyId` exists
  as a stable identifier distinct from the hash).

Design doc will be `docs/08-sim-module.md` when the time comes.
