# 01 — Architecture

## Components

```mermaid
flowchart LR
    subgraph Students
        S[Student apps<br/>X-Api-Key]
    end
    subgraph AWS
        GW[API Gateway<br/>HTTP API]
        AUTH[Lambda authorizer<br/>key + cohort lookup]
        API[API handler Lambda<br/>Python 3.13]
        ADMIN[Admin handler Lambda]
        CONSOLE[Console handler Lambda<br/>session auth]
        DDB[(DynamoDB<br/>data + control tables)]
        SCHED[EventBridge scheduled rules<br/>nightly EOD crons]
        DISP[Dispatcher Lambda]
        Q[SQS ingest queue<br/>+ DLQ]
        ING[Ingest worker Lambda]
        SSM[SSM Parameter Store<br/>upstream API keys]
        SYN[Synthetic generator<br/>library, in-process]
    end
    subgraph Upstreams
        U[Stooq / Yahoo / Finnhub /<br/>Alpha Vantage / CoinGecko / frankfurter]
    end

    S --> GW --> AUTH --> DDB
    GW --> API --> DDB
    GW --> ADMIN --> DDB
    GW --> CONSOLE --> DDB
    API -. cache miss .-> U
    API -. upstream dead .-> SYN
    SCHED --> DISP --> Q --> ING --> U
    ING --> DDB
    ING --> SSM
    API --> SSM
```

### Service choices and why

| Component | Choice | Why |
|---|---|---|
| API front door | **API Gateway HTTP API (v2)** | ~1/3 the cost of REST API; supports Lambda authorizers. We forgo REST-API "usage plans" because our quota model lives in DynamoDB anyway (per-key daily quotas, cohort grouping, instant revoke) — richer than usage plans and portable. |
| Auth | **Lambda authorizer** (payload v2, response caching 300 s) | Looks up hashed key in the control table, attaches key/cohort context to the request. Cached so most requests skip the lookup. |
| Compute | **Seven Python 3.13 Lambdas** (arm64) | `api`, `admin`, `console`, `authorizer`, `dispatcher`, `backfillCoordinator`, and `ingestWorker`. The admin API and the session-authenticated instructor console both shipped in Phase 4. |
| Storage | **DynamoDB, provisioned capacity within the always-free 25 RCU/25 WCU** | EOD-scale data is small (see [03-data-model](03-data-model.md)); provisioned-free beats on-demand pricing at near-zero budget. Switch to on-demand only if throttling appears. |
| Scheduling | **Four EventBridge scheduled rules** | US, India, and FX run after weekday closes; crypto runs daily. |
| Ingest fan-out | **SQS standard queue + DLQ** | Dispatcher enqueues one symbol per message for exact retries. DLQ + redrive gives retry semantics and visibility into failed symbols. |
| Secrets | **SSM Parameter Store `SecureString`** (standard tier, free) | Upstream API keys (Finnhub, Alpha Vantage, CoinGecko Demo). Not Secrets Manager — $0.40/secret/month is the entire monthly budget. |
| Observability | **CloudWatch Logs (14-day retention) + a few alarms** | Structured JSON logs via Lambda Powertools; alarms on DLQ depth, authorizer errors, 5xx rate. |

## Request flows

### 1. Read path (candles / symbols) — cache-first

1. Request hits API Gateway with `X-Api-Key`.
2. Authorizer (or its cache) validates key hash, checks key status, injects
   `keyId`/`cohortId` context. Usage is keyed by the non-secret `keyId`; the key
   hash never leaves the authorizer/control-table lookup path.
3. API Lambda checks + increments the daily quota counter (control table).
4. Query the data table. **EOD candles are immutable once written** — served
   straight from DynamoDB, no upstream call.
5. Unknown symbol → on-demand path: register symbol, serve what can be fetched
   synchronously (bounded to ~3 s), enqueue a historical backfill, return `202`
   with `Retry-After` if nothing is servable yet. See [04-ingestion](04-ingestion.md).

### 2. Quote path — TTL cache with layered fallback

1. Steps 1–3 as above.
2. Look up cached quote item. Fresh (`fetchedAt` is within the application
   freshness window, default 300 s) → return with `meta.source: "cache"`.
3. Stale → try quote-capable adapters in priority order within a strict time
   budget; on success, write-through cache and return `meta.source: "upstream:<name>"`.
4. All upstreams fail/budget exhausted → serve stale cache (`meta.stale: true`),
   or synthesize from last EOD close (`meta.source: "synthetic"`). Never 5xx
   because a free upstream had a bad day.

### 3. Ingest path (nightly)

1. An EventBridge scheduled rule fires per market after close (see [04-ingestion](04-ingestion.md)
   for the cron table).
2. A tiny dispatcher enumerates active symbols for that market and sends one
   seven-day overlap request per SQS message, using SQS API batches of ten.
3. Ingest worker (reserved concurrency = 2, to respect upstream rate limits)
   pulls messages, calls the preferred adapter, normalizes, writes candle
   chunks + updates each symbol's `coverage.eodTo` value.
4. Failures retry via SQS; poisoned messages land in the DLQ and trip an alarm.

Each successful market ingest conditionally advances a small market-status item
(`MARKET#<market>` / `STATUS`). The unauthenticated health route reads those four
items rather than scanning the symbol registry.

### 4. Admin and console paths

The admin API (`admin` Lambda, admin key required) manages cohorts, student
keys, symbols, and backfills — see [05-auth-and-quotas](05-auth-and-quotas.md)
and the [operator CLI guide](operator-cli.md). The instructor console
(`console` Lambda, session bearer token) is a separate, session-authenticated
surface over the same cohort and key logic, built for admins and instructors
who manage classes day to day without the CLI — see [08-console](08-console.md).
Curated backfills are still started by the operator-only
`scripts/enqueue_backfill.py` command; exact unknown symbols use the API's
lease-protected lazy-backfill path.

## Tenets

- **Cache-first, upstream-second, synthetic-last.** Students should almost never
  wait on a third-party API.
- **Upstreams are untrusted.** Every adapter call has a timeout, a per-source
  daily/minute budget tracked in the control table, and a circuit breaker.
- **Immutable observations.** Once an EOD candle is accepted, its raw values do
  not change. Corporate-action metadata may change, so read-time `adjclose` may
  change without rewriting the raw candle.
- **One region:** `eu-west-2`, deployed through the `megh.io` shared AWS profile.
  No multi-region ambitions.
- **Two IaC tools, one owner per resource.** Terraform owns durable DynamoDB
  data and the admin allowlist. Serverless owns compute, API Gateway, and its
  transient application queue. See [06-infrastructure](06-infrastructure.md).
