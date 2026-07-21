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
- **Circuit breaker** — three consecutive failures pause a source for 15 min;
  calls may probe it again after the cooldown.
- **Timeouts** — 3 s per upstream call on the request path, 10 s on ingest paths.

## Source portfolio (v1)

| Adapter | Key needed | Free limit (approx) | Used for | Risk notes |
|---|---|---|---|---|
| **Stooq** | No | CSV endpoint currently requires a browser proof-of-work check | Deferred pending a supported machine-to-machine path | Phase 1 does not bypass the browser check observed in July 2026. |
| **Yahoo Finance** (unofficial) | No | Unofficial; throttles/breaks without notice | Primary US EOD backfill and nightly ingest; later NSE/BSE and quotes | **Fragile & ToS-grey.** Isolated behind one adapter, low volume, and protected by a circuit cooldown. |
| **Finnhub** | Yes (SSM) | 60 req/min | US real-time-ish quotes | Solid free tier; key already held |
| **Alpha Vantage** | Optional SSM key | [25 requests/day](https://www.alphavantage.co/support/) | Limited emergency US EOD fallback | Missing key disables only this fallback; its tiny quota cannot cover the full universe during a Yahoo-wide outage. |
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

The Phase 2 EventBridge schedule is defined on the dispatcher Lambda in
`serverless.yml`:

| Schedule | Cron | Universe slice |
|---|---|---|
| US close | `cron(30 23 ? * MON-FRI *)` | Active US equities + ETFs |

India, FX, crypto, and quote warming are Phase 3 or later.

Worker behavior: reserved concurrency 2; for each one-symbol message it picks the
first capability-matching adapter with budget, fetches, normalizes (dates to
exchange-local trading dates, numbers to `Decimal`), upserts the current month
chunk, and advances `coverage.eodTo` on the symbol's `META` item. A failure for
one symbol retries only that SQS record. Lambda partial-batch responses return
only failed records. Candle writes are idempotent; job completion uses a
conditional state transition so retries cannot over-count.

Chunk upserts use the optimistic `revision` merge described in
[03-data-model](03-data-model.md), because duplicate SQS deliveries and an
overlapping current-year backfill can otherwise lose candles. Synthetic data is
never an ingest result: after all real adapters fail, the record retries and may
reach the DLQ; synthesis remains exclusively on the API read path.

Two versioned queue payloads are sufficient:

```json
{ "v": 1, "kind": "eod_batch", "market": "US", "symbol": "AAPL",
  "from": "2026-07-15", "to": "2026-07-21" }
```

```json
{ "v": 1, "kind": "backfill_year", "jobId": "job_123",
  "symbol": "AAPL", "year": 2020 }
```

For Frankfurter reference rates, normalization produces a close-only candle
(`open == high == low == close`) with `volume = null`. Other adapters may also
return null volume when the upstream has no meaningful figure.

### Corporate actions (splits & dividends)

Corporate-action ingestion remains a Phase 3 task. Through Phase 2, no `ADJ`
items are produced, so `adjclose` equals raw `close`. Alpha Vantage uses the raw
`TIME_SERIES_DAILY` function; its adjusted endpoint is premium.

## Historical backfill (10+ years)

- Triggered by `scripts/enqueue_backfill.py`. Admin HTTP triggering and lazy
  unknown-symbol backfills are deferred.
- A `JOB#` item tracks symbol-year work-unit totals; one SQS message and one
  `WORK#<symbol>#YEAR#<YYYY>` state item per unit keep invocations small and
  resumable—a failed year retries alone.
- The checked-in `us-v1` snapshot contains 503 S&P 500 securities plus 12
  explicit ETFs. Its initial 10-year backfill is dominated by Yahoo
  politeness delays, not compute: budget ~2–3 hours wall-clock at concurrency 2.
  Run once per environment, then it's nightly deltas forever.

## On-demand (lazy) path — Phase 3

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

## Synthetic fallback (`src/synthetic/`) — Phase 3

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

- Phase 2 rejects rows unless prices are finite and positive,
  `low ≤ open,close ≤ high`, volume is null or non-negative, dates are unique
  and within the requested range, and the source is present. The action-aware
  large-move check is deferred with corporate actions to Phase 3.
- Conflicting values between sources: first-written wins (immutability tenet);
  discrepancies logged with both values for spot-checking.
- Every persisted candle row stores `src` (adapter name) because fallback sources
  may differ within one monthly chunk.
