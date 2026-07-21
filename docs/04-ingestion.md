# 04 — Ingestion & Sources

## Source adapter layer

Every upstream lives behind one Python interface; nothing outside `src/adapters/`
knows any vendor's shape.

```python
class SourceAdapter(Protocol):
    name: str
    def capabilities(self) -> set[Capability]:
        """ {EOD_US, EOD_IN, EOD_FX, EOD_CRYPTO, QUOTE_US, QUOTE_IN, QUOTE_FX, QUOTE_CRYPTO, DISCOVERY} """
    def get_eod(self, symbol: str, start: date, end: date) -> EodResult: ...
    def get_quote(self, symbol: str) -> Quote: ...
    def discover(self, symbol: str) -> SymbolMetadata | None: ...
```

`EodResult` contains `candles: list[Candle]` and
`actions: list[CorporateAction]`. Adapters that do not supply corporate actions
return an empty action list. This keeps vendor-specific combined candle/event
responses inside the adapter without adding a separate abstraction.

Unsupported methods raise a shared `CapabilityUnavailable` exception. Canonical
symbols are translated to vendor identifiers inside each adapter; optional
`adapterHints` on the registry item cache identifiers such as a CoinGecko coin
ID. No caller contains vendor-specific symbol rules.

Shared adapter machinery (in `src/shared/`):

- **Rate budgets** — atomic counters in the control table per adapter per window
  (e.g. Alpha Vantage `25/day`, Finnhub `55/min` — set slightly under the
  published free limits). A call first reserves budget; no budget → skip to the
  next adapter in the chain.
- **Circuit breaker** — N consecutive failures opens the circuit for 15 min;
  half-open probes one call.
- **Timeouts** — 3 s per upstream call on the request path, 10 s on ingest paths.

## Source portfolio (v1)

| Adapter | Key needed | Free limit (approx) | Used for | Risk notes |
|---|---|---|---|---|
| **Stooq** | No | CSV endpoint currently requires a browser proof-of-work check | Deferred pending a supported machine-to-machine path | Phase 1 does not bypass the browser check observed in July 2026. |
| **Yahoo Finance** (unofficial) | No | Unofficial; throttles/breaks without notice | Phase 1 US EOD backfill; later NSE/BSE EOD + quotes, US quote fallback, symbol discovery | **Fragile & ToS-grey.** Isolated behind one adapter and always backed by graceful degradation; an official India fallback is planned for v1.1. Educational use, low volume, respectful backoff. |
| **Finnhub** | Yes (SSM) | 60 req/min | US real-time-ish quotes | Solid free tier; key already held |
| **Alpha Vantage** | Yes (SSM) | 25 req/day | Emergency EOD/FX fallback only | Tiny quota — last in every chain |
| **CoinGecko** | Yes (free Demo key in SSM) | Demo-plan budget configured from current published limits | Recent crypto EOD + quotes | Public historical access is limited to 365 days; Yahoo supplies deep-history backfill |
| **frankfurter.dev** (ECB rates) | No | Unmetered | FX EOD reference rates | ECB daily fixes; EUR-based, cross-rates computed |

### Fallback chains

| Need | Chain |
|---|---|
| US EOD ingest | Yahoo → Alpha Vantage → retry/DLQ (Stooq paused pending a supported machine endpoint) |
| India EOD ingest | Yahoo → retry/DLQ (NSE bhavcopy is deferred to v1.1) |
| FX EOD ingest | frankfurter → Yahoo → Alpha Vantage → retry/DLQ |
| Crypto recent EOD ingest | CoinGecko → Yahoo → retry/DLQ |
| Crypto deep-history backfill | Yahoo → retry/DLQ |
| US quote | Finnhub → Yahoo → cached-stale → synthetic |
| India quote | Yahoo → cached-stale → synthetic |
| FX / crypto quote | frankfurter (daily) / CoinGecko → Yahoo → cached-stale → synthetic |

## Scheduled bulk ingest

EventBridge cron schedules (defined as Serverless Framework `schedule` events on
the dispatcher Lambda, all UTC), which fans out to SQS in ~25-symbol batches:

| Schedule | Cron | Universe slice |
|---|---|---|
| US close | `30 21 * * MON-FRI` (≈17:30 ET soft close +1h buffer; DST drift accepted, documented) | US equities + ETFs |
| India close | `30 11 * * MON-FRI` (≈17:00 IST) | NSE/BSE |
| FX daily | `0 17 * * MON-FRI` (after ECB ~16:00 CET fix) | FX pairs |
| Crypto daily | `15 0 * * *` | Crypto (UTC day close) |
| Quote warmer *(optional, off by default)* | every 5 min during market hours | Top ~50 symbols — pre-warms the quote cache before a class demo |

Worker behavior: reserved concurrency 2; for each symbol in a batch it picks the
first capability-matching adapter with budget, fetches, normalizes (dates to
exchange-local trading dates, numbers to `Decimal`), upserts the current month
chunk, and advances `coverage.eodTo` on the symbol's `META` item. A failure for
one symbol does not discard successful symbols. Lambda partial-batch responses
return only failed SQS records for retry. Candle writes are idempotent; job
completion uses a conditional state transition so retries cannot over-count.

Chunk upserts use the optimistic `revision` merge described in
[03-data-model](03-data-model.md), because duplicate SQS deliveries and an
overlapping current-year backfill can otherwise lose candles. Synthetic data is
never an ingest result: after all real adapters fail, the record retries and may
reach the DLQ; synthesis remains exclusively on the API read path.

Two versioned queue payloads are sufficient:

```json
{ "v": 1, "kind": "eod_batch", "market": "US",
  "symbols": ["AAPL", "MSFT"], "date": "2026-07-20" }
```

```json
{ "v": 1, "kind": "backfill_year", "jobId": "job_123",
  "symbol": "AAPL", "year": 2020 }
```

For Frankfurter reference rates, normalization produces a close-only candle
(`open == high == low == close`) with `volume = null`. Other adapters may also
return null volume when the upstream has no meaningful figure.

### Corporate actions (splits & dividends)

The nightly equity jobs also refresh each symbol's `ADJ` item (see
[03-data-model](03-data-model.md)). Yahoo's chart API returns split and dividend
events alongside candles (`events=div,splits`) through `EodResult`; for
Stooq-sourced US symbols, a weekly Yahoo sweep (`0 6 * * SAT`) fetches events.
Alpha Vantage's adjusted endpoint is premium and is not a zero-cost v1 fallback.
Ingestion resolves and stores each event's one-event adjustment factor (and the
pre-event reference close for dividends), so reads never need candles outside
their requested range. The `ADJ` write is a full-item replace (small list,
idempotent). Historical backfills fetch the symbol's complete action history once.

## Historical backfill (10+ years)

- Triggered per symbol-set via `POST /v1/admin/ingest/backfill` or the CLI; also
  auto-enqueued (last 10 y) when an unknown symbol is first requested.
- A `JOB#` item tracks symbol-year work-unit totals; one SQS message and one
  `WORK#<symbol>#YEAR#<YYYY>` state item per unit keep invocations small and
  resumable—a failed year retries alone.
- Initial full backfill (~700 symbols × 10 y) is dominated by Yahoo
  politeness delays, not compute: budget ~2–3 hours wall-clock at concurrency 2.
  Run once per environment, then it's nightly deltas forever.

## On-demand (lazy) path

Request for a symbol not in the registry:

1. Validate shape against the symbol scheme; reject garbage with `404` fast.
2. Probe the discovery adapter (Yahoo lookup) to confirm existence; create the
   `META` item with `active: true, discovered: true`.
3. Quotes: attempt synchronously within the 3 s budget; if it fails and there is
   no real close to anchor a synthetic quote, enqueue backfill and return `202`.
4. Candles: enqueue backfill, return `202 BACKFILL_IN_PROGRESS` + `Retry-After: 60`.
5. Discovered symbols join the environment's nightly universe automatically,
   capped at 300 active discovered symbols. When full, the oldest `discoveredAt`
   entry leaves the cron universe but remains stored. The cap is global because
   the registry and ingest pipeline are global, not cohort-owned.

Until a registered symbol has at least one real candle, candle requests keep
returning `202`; synthetic history is not invented from an arbitrary starting
price.

## Synthetic fallback (`src/synthetic/`)

Purpose: keep already-initialized class symbols usable during an upstream outage.
This is not a simulation product; it is an always-flagged degradation layer.

- **Determinism:** geometric Brownian motion uses a daily shock derived from
  `sha256(symbol + date)`. Starting at the last real close before a gap, prices
  are generated recursively through eligible dates. Every student querying the
  same stored dataset sees the same result; later real data replaces synthetic
  values and may become the anchor for a remaining gap.
- **Calibration:** anchored to the last real close and annualized volatility
  estimated from up to 90 trailing real candles (defaults when history is too
  short: σ=25 % equity, 10 % FX, 70 % crypto, μ=5 %).
- **Gap policy:** synthetic candles are generated **on read, never persisted** —
  real data landing later must win. Equities, ETFs, and FX use weekdays; crypto
  uses UTC calendar days. This intentionally simple teaching calendar may include
  exchange holidays. Each generated response candle carries `"synthetic": true`
  (`meta.source: "synthetic"` or `"mixed"`).
- **No backward synthesis:** a gap can only be generated from a real close before
  it. If the request begins before the earliest real candle, the response starts
  at the earliest available real candle and includes `meta.partial: true` plus
  `meta.availableFrom`. If a backfill for that leading range is still active,
  return `202 BACKFILL_IN_PROGRESS` instead.
- **Quote synthesis:** last real close + deterministic intraday walk seeded by
  `(symbol, date, 5-min bucket)` so a "live" demo ticks plausibly even fully
  offline.

## Data quality rules

- Reject upstream rows failing sanity checks: `low ≤ open,close ≤ high`,
  `high > 0`, volume is null or ≥ 0, |day-over-day close change| < 60 % — unless the `ADJ`
  item shows a split on that date, which legitimizes the jump. A large move with
  **no** known action is logged for review (usually a missed split — the check
  doubles as corporate-action QA).
- Conflicting values between sources: first-written wins (immutability tenet);
  discrepancies logged with both values for spot-checking.
- Every persisted candle row stores `src` (adapter name) because fallback sources
  may differ within one monthly chunk.
