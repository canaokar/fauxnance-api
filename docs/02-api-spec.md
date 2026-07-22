# 02 — API Specification

Base URL:
`https://y4t9nq2bqf.execute-api.eu-west-2.amazonaws.com/v1` (the raw API
Gateway URL; no custom domain is currently configured).
All endpoints require the `X-Api-Key` header unless noted. All v1 responses are
JSON, UTF-8. CSV candle export is deferred to v1.x. A machine-readable
`docs/openapi.yaml` will be generated from this spec in Phase 1 and is the
contract of record once it exists.

## Symbol scheme

One unified namespace across asset classes:

| Asset class | Format | Examples |
|---|---|---|
| US equity/ETF | Plain ticker | `AAPL`, `SPY`, `BRK.B` |
| NSE equity | Ticker + `.NS` | `INFY.NS`, `RELIANCE.NS` |
| BSE equity | Ticker + `.BO` | `TATASTEEL.BO` |
| FX pair | `FX:` + base+quote | `FX:EURUSD`, `FX:USDINR` |
| Crypto | `X:` + base-quote | `X:BTC-USD`, `X:ETH-USD` |

Symbols are case-insensitive on input, canonicalized to upper-case in responses.

## Response envelope

Every JSON success response has `data` and `meta`. `asOf` and `disclaimer` are
always present. `symbol` and `source` appear on market-data responses;
`stale` appears on quote responses. They are not padded onto usage, health,
collection, or admin responses where they have no meaning.

```json
{
  "data": { },
  "meta": {
    "symbol": "AAPL",
    "source": "stored | cache | upstream:<name> | synthetic | mixed",
    "stale": false,
    "asOf": "2026-07-20T20:00:00Z",
    "disclaimer": "Educational data. Not for investment use."
  }
}
```

- `meta.source` is `synthetic` when every returned market value was generated.
  Candle ranges that combine real and generated rows report `mixed`, and each
  generated candle carries `"synthetic": true`.
- `meta.stale: true` means the cached value is past its application freshness
  window but upstreams were unavailable.
- Candle responses use `stored` when all rows are persisted observations.
  Quote responses use `cache`, `upstream:<name>`, or `synthetic`.
- For market data, `asOf` is the newest observation timestamp/date represented;
  elsewhere it is the response-generation timestamp.
- Candle responses add `partial: true` and `availableFrom` to `meta` only when
  the requested range starts before the earliest real candle and no leading
  backfill is still active. Synthetic data is never generated backwards.

Every Fauxnance Lambda-generated error response:

```json
{
  "error": {
    "code": "RATE_LIMITED",
    "message": "Daily quota of 2000 requests exhausted. Resets at 00:00 UTC.",
    "details": { }
  }
}
```

This envelope applies to responses produced by Fauxnance Lambdas. API Gateway
can reject a request before a Lambda runs (for example, a missing authorizer
identity source or stage-level throttling); those responses use API Gateway's
standard error body and status. Clients must primarily branch on HTTP status and
may use `error.code` when the body is a Fauxnance error.

## Public endpoints

### `GET /v1/symbols` *(planned for Phase 4)*
Search/browse the symbol registry.

| Query param | Type | Notes |
|---|---|---|
| `q` | string | Symbol prefix (case-insensitive) |
| `type` | enum | `equity \| etf \| fx \| crypto` |
| `exchange` | enum | `NYSE \| NASDAQ \| NSE \| BSE \| FX \| CRYPTO` |
| `cursor` | string | Opaque pagination cursor |
| `limit` | int | Default 50, max 200 |

Response `data`: `{ "symbols": [ {symbol, name, type, exchange, currency, active} ], "cursor": "..." }`

### `GET /v1/symbols/{symbol}`
Registry entry for one symbol, plus coverage info:

```json
{
  "symbol": "INFY.NS", "name": "Infosys Ltd", "type": "equity",
  "exchange": "NSE", "currency": "INR", "active": true,
  "coverage": { "eodFrom": "2015-01-01", "eodTo": "2026-07-20" }
}
```

`404 SYMBOL_NOT_FOUND` if unknown **and** on-demand registration fails
(pattern invalid / no upstream recognizes it).

### `GET /v1/candles/{symbol}`
Historical EOD OHLCV.

| Query param | Type | Notes |
|---|---|---|
| `from` | date (`YYYY-MM-DD`) | Default: 1 year ago |
| `to` | date | Default: today |
| `interval` | enum | `1d` only in v1. `1wk`/`1mo` are computed server-side in v1.1. |

Response `data`:

```json
{
  "symbol": "AAPL", "interval": "1d", "currency": "USD",
  "candles": [
    { "date": "2026-07-20", "open": 231.1, "high": 233.9, "low": 229.8,
      "close": 232.5, "adjclose": 232.5, "volume": 51234567,
      "synthetic": false }
  ]
}
```

- **Adjustment — Yahoo Finance convention.** Every candle carries both `close`
  (raw, as traded that day) and `adjclose` (split- **and** dividend-adjusted,
  suitable for return calculations and backtests). `volume` is raw. When no
  corporate actions are known for a symbol (or for FX/crypto, where the concept
  doesn't apply), `adjclose == close`. Adjustment is computed **at read time**
  from stored corporate-action factors (see [03-data-model](03-data-model.md)) —
  stored candles remain raw and immutable; a newly ingested split changes
  `adjclose` in responses without rewriting history.
- Max range per request: 10 years (full depth). Responses above ~5k candles are
  rejected with `400 RANGE_TOO_LARGE` (cannot happen with `1d` + 10-year cap;
  guard exists for future intervals).
- `from` and `to` are inclusive. Equities, ETFs, and FX return eligible weekdays;
  crypto returns UTC calendar days. The deliberately simple synthetic calendar
  may include an exchange holiday and is acceptable for this teaching service.
- `volume` is nullable. For a daily FX reference rate, the normalized candle has
  `open == high == low == close`, `volume: null`, and `adjclose == close`.
- Unknown-but-valid symbol with backfill in flight → `202` with
  `{ "error": { "code": "BACKFILL_IN_PROGRESS" } }` and `Retry-After: 60`.

### `GET /v1/quotes/{symbol}`
Latest quote (delayed / best-effort; see freshness rules in
[01-architecture](01-architecture.md)).

Response `data`:

```json
{
  "symbol": "AAPL", "price": 232.71, "currency": "USD",
  "change": 0.21, "changePercent": 0.09,
  "previousClose": 232.50, "asOf": "2026-07-21T15:42:10Z",
  "marketState": "open | closed | pre | post | unknown"
}
```

`changePercent` is expressed in percentage points (`0.09` means `0.09%`, not
`9%`). Quote freshness is decided from `fetchedAt`, not DynamoDB's asynchronous
TTL deletion. A newly discovered symbol for which neither a real quote nor a real
closing-price anchor is available returns `202 BACKFILL_IN_PROGRESS`.

### `GET /v1/quotes?symbols=AAPL,INFY.NS,FX:EURUSD`
Batch quotes, max 25 symbols. Per-symbol results; failures are per-item:

```json
{
  "quotes": [
    { "symbol": "AAPL", "source": "cache", "stale": false,
      "quote": { "price": 232.71, "currency": "USD",
        "asOf": "2026-07-21T15:42:10Z", "marketState": "open" } },
    { "symbol": "BAD", "error": { "code": "SYMBOL_NOT_FOUND",
      "message": "Symbol was not recognized.", "details": {} } }
  ]
}
```

Each entry has exactly one of `quote` or `error`; successful entries also carry
their own `source` and `stale` because a batch can mix cache, upstream, stale,
and synthetic results. The outer HTTP response remains `200` when individual
symbols fail; request-level validation/auth failures use the normal HTTP error
response.

Counts as **one** request against quota regardless of batch size (keeps student
dashboards cheap; documented so they batch deliberately).

### `GET /v1/usage`
The caller's own quota status. Response `data`:

```json
{ "keyLabel": "team-3", "cohort": "HDFC-GradBatch-2026Q3",
  "dailyQuota": 2000, "usedToday": 27, "resetsAt": "2026-07-22T00:00:00Z" }
```

### `GET /v1/health`  *(no auth)*
Liveness + data freshness summary per market. Used by students to rule out
"is it me or the API". It always returns `200` while the API itself is running;
stale markets make `data.status` `degraded`, not an HTTP failure. Response `data`:

```json
{ "status": "ok", "markets": [
  { "market": "US", "latestEod": "2026-07-20", "stale": false }
] }
```

The handler reads one `MARKET#<market>` / `STATUS` item per configured market;
ingestion conditionally advances `latestEod`. It does not scan symbol metadata.

## Admin endpoints (admin key required)

| Method + path | Purpose |
|---|---|
| `POST /v1/admin/cohorts` | Create cohort `{name, defaultDailyQuota, expiresAt}` |
| `GET /v1/admin/cohorts` | List cohorts with aggregate usage |
| `POST /v1/admin/keys` | Issue student keys: `{cohortId, count \| labels[], dailyQuota?}` → returns `{keyId, label, key}` entries; plaintext keys appear **once** |
| `GET /v1/admin/keys?cohortId=` | List keys (`keyId`, label, usage, status — never hash or plaintext) |
| `DELETE /v1/admin/keys/{keyId}` | Revoke immediately (authorizer cache ≤ 300 s lag) |
| `POST /v1/admin/symbols` | Add symbols to the curated universe |
| `POST /v1/admin/ingest/backfill` | Trigger historical backfill `{symbols[] \| universe, from, to}` → `jobId` |
| `GET /v1/admin/ingest/jobs/{jobId}` | Backfill progress `{state, done, failed[], total}` where counts and failures are symbol-year work units |

## Error codes

| HTTP | `code` | When |
|---|---|---|
| 400 | `VALIDATION_ERROR` | Bad params, malformed symbol, bad date range |
| 400 | `RANGE_TOO_LARGE` | Candle range over limit |
| 401 | *(API Gateway standard body)* | Missing/unknown/revoked key rejected by the authorizer |
| 403 | *(API Gateway standard body)* | Expired/inactive key or cohort rejected by the authorizer |
| 403 | `ADMIN_ONLY` | Non-admin key on `/admin/*` |
| 404 | `SYMBOL_NOT_FOUND` | Unknown symbol, registration failed |
| 202 | `BACKFILL_IN_PROGRESS` | Data being fetched; retry later |
| 429 | `RATE_LIMITED` | Daily quota exhausted (`Retry-After` header set) |
| 503 | `UPSTREAM_UNAVAILABLE` | Exact symbol discovery could not reach its upstream; retry later |
| 500 | `INTERNAL_ERROR` | Bug (alarmed) |

## Versioning & compatibility

- Path-versioned (`/v1/`). Additive changes (new fields, new endpoints) are
  non-breaking and can ship anytime. Breaking changes require `/v2/`.
- `/v1/sim/*` is **reserved** for the future paper-trading module — v1 handlers
  must 404 that prefix without matching other routes.
