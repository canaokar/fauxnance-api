# 03 — Data Model (DynamoDB)

Two tables, deliberately separated: **`fauxnance-data`** (market data — big, mostly
immutable, read-heavy) and **`fauxnance-control`** (keys, cohorts, quotas, jobs,
source budgets — small, write-heavy). Separation keeps IAM tight (the public API
can read market data and write only quote-cache plus on-demand registry items)
and lets capacity be tuned independently within the free 25 RCU / 25 WCU budget.

## Table: `fauxnance-data`

Single-table design. `PK`/`SK` are generic strings.

### Item types

| Item | PK | SK | Notes |
|---|---|---|---|
| Symbol registry | `SYM#<symbol>` | `META` | name, type, exchange, currency, active, coverage dates, `discovered`, `discoveredAt`, adapterHints |
| Candle chunk | `SYM#<symbol>` | `EOD#<YYYY-MM>` | **One item per symbol-month**, `candles`: list of `{d,o,h,l,c,v,src}` plus integer `revision`; `v` may be null where the source has no volume |
| Quote cache | `SYM#<symbol>` | `QUOTE` | Latest quote + `fetchedAt`; `expiresAt` defaults to seven days for cleanup and is not the freshness decision |
| Corporate actions | `SYM#<symbol>` | `ADJ` | List of `{date, type: split\|dividend, value, factor, referenceClose?}`, newest first. `date` is the ex-date and `factor` applies to earlier candles. Absent for FX/crypto and action-free symbols. |
| Symbol index | `SYMBOLS` | `<symbol>` | Thin projection for prefix search, browse/filter, and dispatcher universe enumeration |
| Market status | `MARKET#<market>` | `STATUS` | `latestEod`, `updatedAt`; conditionally advanced by successful ingestion and read by `/health` |

### Why month-chunked candles

An EOD candle is roughly 60 bytes before DynamoDB/attribute overhead. Storing one
item per day creates about 2,600 items and write requests per 10-year backfill.
Chunking by month (~22 candles, approximately 1.5 KB/item):

- 10-year range query = ~120 items ≈ 180 KB before response serialization → one
  `Query` page, roughly 23 eventually consistent RCUs (about 45 if strong).
- Whole 10-year backfill for one symbol = 120 item writes instead of 2,600.
- Chunks stay far below the 400 KB item limit and align with ingest batching.

DynamoDB read capacity is based mainly on bytes read, so chunking improves request
count, write cost, and latency rather than making the same candle bytes free to
read.

Trade-off: ingestion does a read-modify-write on a month chunk. SQS delivery is
at-least-once and a current-year backfill can overlap the nightly job, so the
writer merges by candle date, preserves an existing date (first-written wins),
and conditionally writes `revision = previous_revision + 1`. A failed condition
re-reads, re-merges, and retries with jitter. This is the only concurrency
control required for candle chunks.

### Read-time adjustment (`adjclose`)

Stored candles are raw and immutable; the `ADJ` item is the only thing corporate
actions touch. On a candle read, the handler gets `ADJ` in parallel with the
chunk `Query` and computes, per candle:

```
factor(date) = ∏ over actions after `date` of action.factor
adjclose = close × factor(date)
```

At ingestion, a split factor is `1 / ratio` (4:1 → `0.25`). A dividend factor is
`(referenceClose - amount) / referenceClose`, where `referenceClose` is the last
raw close before the ex-date; both the reference close and resulting factor are
stored on the action. This makes a candle-range read self-contained even when
the reference candle falls outside the requested range. Cumulative factors are
memoized per request (one pass over the action list). A newly ingested action
changes future *responses* only—no stored candle is rewritten.

### Access patterns → queries

| Pattern | Query |
|---|---|
| Candles for symbol in `[from,to]` | `Query PK=SYM#X, SK BETWEEN EOD#<from-month> AND EOD#<to-month>`, trim edges in code; parallel `GetItem SK=ADJ` for adjustment factors |
| Symbol detail | `GetItem PK=SYM#X, SK=META` |
| Latest quote | `GetItem PK=SYM#X, SK=QUOTE` |
| Symbol search by prefix | `Query PK=SYMBOLS, SK begins_with(<prefix>)` — v1 deliberately supports symbol-prefix search only |
| Browse/list by exchange/type | Paginated `Query PK=SYMBOLS` with a filter over the thin projection; cheap at the v1 universe size |
| Enumerate a market universe | Paginated `Query PK=SYMBOLS`, filter `active` and market/exchange; the dispatcher never scans candle items |
| Market freshness | `GetItem PK=MARKET#<market>, SK=STATUS` (four reads for `/health`) |

### Size estimate

Universe ≈ 700 symbols (S&P 500 + NIFTY 100 + ~40 ETFs + ~30 FX pairs + ~30 crypto).
700 symbols × 120 month-chunks × 1.5 KB ≈ **130 MB** — 0.5 % of the free 25 GB.
Even 5,000 symbols stays under 1 GB. Storage is a non-issue; RCU/WCU discipline is
the actual constraint, hence chunking.

## Table: `fauxnance-control`

| Item | PK | SK | Notes |
|---|---|---|---|
| API key | `KEY#<sha256(key)>` | `META` | keyId, label, type (`student\|admin`), status, createdAt, expiresAt; student keys also have cohortId + dailyQuota |
| Key ID lookup | `KEYID#<keyId>` | `META` | key hash + cohortId; admin-only reverse lookup used for revocation |
| Key usage counter | `KEYID#<keyId>` | `USAGE#<YYYY-MM-DD>` | `count` (atomic ADD), TTL = date + 35 d; lets handlers use authorizer context without receiving the key hash |
| Cohort | `COHORT#<id>` | `META` | name, defaultDailyQuota, expiresAt, status |
| Cohort key index | `COHORT#<id>` | `KEY#<keyId>` | For "list keys in cohort" without a GSI |
| Source budget | `SRC#<adapter>` | `BUDGET#<window>` | Atomic counters per upstream (per-day / per-minute windows), TTL'd — enforces free-tier limits |
| Circuit breaker | `SRC#<adapter>` | `STATE` | `closed\|open\|halfOpen`, openedAt, failureCount |
| Ingest job | `JOB#<jobId>` | `META` | type, params, state, totals, startedAt |
| Ingest job item | `JOB#<jobId>` | `WORK#<symbol>#YEAR#<YYYY>` | per-symbol-year state; state transitions are conditional so SQS retries cannot double-count completion |

Key issuance writes the hashed key, `KEYID#` lookup, and cohort index in one
transaction. Revocation resolves `keyId` through `KEYID#` and updates the hashed
key item; public/admin responses never expose the hash.

To report cohort usage, the admin handler queries the cohort key index and
batch-gets today's `KEYID#<keyId>` usage items. No usage scan or pre-aggregation
is required at the expected cohort size.

### Quota mechanics

- Fixed daily window (00:00 UTC). Using the `keyId` from authorizer context, the
  API Lambda atomically sets
  `count = if_not_exists(count, 0) + 1` with condition
  `attribute_not_exists(count) OR count < :quota`; `ConditionalCheckFailed` →
  `429`. One WCU per request is the dominant write load and the reason `control`
  gets the larger WCU share.
- Authorizer result caching (300 s) keeps key lookups to ~1 read per student per
  5 minutes; quota increments are the per-request cost.

## Capacity plan (within always-free 25 RCU / 25 WCU)

| Table | RCU | WCU | Sizing logic |
|---|---|---|---|
| `fauxnance-data` | 15 | 10 | A full 10-year candle query is roughly a 23-RCU eventual-consistency burst; low average traffic and burst capacity should absorb normal use, but Phase 1 must load-test this assumption |
| `fauxnance-control` | 10 | 15 | 50 keys exhausting 2,000/day average about 1.2 writes/s; short class-start bursts rely on DynamoDB burst capacity and SDK retries and must be included in the Phase 1 load check |

Throttling response: retries with jitter in the SDK, plus a documented escape
hatch — flip either table to on-demand via a Terraform variable if a cohort
outgrows free tier (cost then ~$1–3/mo, still trivial).

## Streams / TTL

- **TTL enabled** on `control` (usage counters, source budgets) and on the
  `QUOTE` items in `data`. DynamoDB TTL is asynchronous cleanup only; quote
  freshness always compares `fetchedAt` with the configured freshness window.
- **No DynamoDB Streams in v1** — nothing consumes them. The future sim module
  may add a stream for portfolio mark-to-market; noted in [07-roadmap](07-roadmap.md).
