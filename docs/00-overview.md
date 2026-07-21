# 00 — Overview

## Problem

Graduate trainees at banks build capstone projects — enterprise trading platforms,
portfolio managers, asset managers — and consistently struggle to find usable market
data. Good APIs are paid; free ones are rate-limited, fragmented, or unreliable.
Instructors spend class time debugging data access instead of teaching.

## Solution

**Fauxnance** is a single, free, instructor-operated market-data API. It aggregates
several free upstream sources, normalizes and stores the data in AWS, and exposes a
clean versioned REST API behind per-student API keys. If every upstream source dies
mid-class, registered symbols with stored history can fall back to deterministic,
clearly flagged synthetic data so a training session does not stall.

## Goals

1. **Zero cost to students, near-zero cost to operate.** Design fits the AWS
   always-free tier and free upstream API tiers. Target < $2/month.
2. **Coverage that matches the capstones:** US equities + ETFs, Indian equities
   (NSE/BSE), FX pairs, and major crypto.
3. **Data types:** 10+ years of historical EOD OHLCV, plus latest (delayed) quotes.
4. **Reliability over freshness.** EOD data is the backbone; quotes are best-effort
   and cached. Synthetic fallback keeps already-initialized symbols usable during
   an upstream outage; newly discovered symbols may return `202` while backfilling.
5. **Teachable ops.** Per-student API keys, quotas, and error semantics mirror real
   commercial APIs, so using Fauxnance itself teaches API hygiene.
6. **Scale-ready schema.** Built for one cohort (~30–50 students) but the key/cohort
   and data models must scale to multiple parallel cohorts without rework.

## Non-goals (v1)

- **No intraday/tick history.** EOD candles + latest quote only.
- **No fundamentals or news.** (Candidates for v2.) Corporate actions (splits,
  dividends) *are* in scope, but only as adjustment factors powering `adjclose` —
  there is no standalone actions endpoint in v1.
- **No paper-trading/simulation endpoints.** Explicitly deferred — the URL namespace
  `/v1/sim/*` is reserved so a simulation module can bolt on later without breaking
  changes (see [07-roadmap](07-roadmap.md)).
- **No guarantees of data accuracy.** This is a teaching tool, not an investment
  data product. Every successful response says so (`meta.disclaimer`).
- **No self-serve signup.** Keys are issued by the instructor via admin API/CLI.

## Key decisions (locked during spec interview, 2026-07-21)

| Decision | Choice | Rationale |
|---|---|---|
| Coverage | US equities/ETFs, NSE/BSE, FX, crypto | Matches bank capstone briefs |
| Data types | Historical EOD OHLCV + delayed quotes | Backbone of backtesting + "live" sims |
| History depth | 10+ years | Covers full market cycles; cheap at EOD granularity |
| Runtime | Python 3.13 Lambdas | Data-wrangling ecosystem, trainee familiarity |
| Auth | Per-student API keys, DynamoDB-backed | Real-world hygiene, per-key revoke/quota |
| Key admin | Admin API + operator CLI | Flexible mid-class, no self-serve abuse surface |
| Ingestion | Hybrid: nightly bulk of curated universe + on-demand fetch-and-cache | Predictable upstream usage, no 404s for odd symbols |
| Synthetic data | Anchored fallback with explicit `source: "synthetic"` flag | Registered symbols remain usable when a free upstream is down without inventing an arbitrary starting price |
| Price adjustment | Yahoo convention: raw `close` + `adjclose` per candle, computed at read time from stored split/dividend factors *(added 2026-07-21)* | Matches the format every tutorial uses; keeps stored candles immutable |
| Upstreams | Stooq, Yahoo (unofficial), Finnhub, Alpha Vantage, CoinGecko, frankfurter.dev | Portfolio of free tiers; adapter layer isolates each |
| Budget | Near-zero / free tier | Personal AWS account, training side project |
| Scope | Data API now, simulation later | Students building trading logic *is* the capstone |
| IaC | Serverless Framework for compute/API wiring; one flat Terraform root for Phase 1 persistence | Keeps `sls remove` away from stored data without adding infrastructure layers; see [06-infrastructure](06-infrastructure.md) |

> **Note on IaC:** the **Serverless Framework** owns the Phase 1 Lambdas and HTTP
> API; **Terraform** owns the two DynamoDB tables and admin allowlist. The dev
> table names are deterministic, so no handoff layer is needed. Future resources
> are added only when their roadmap phase is implemented. See
> [06-infrastructure](06-infrastructure.md).

## Repository layout (target)

```
fauxnance-api/
├── docs/              # All design docs + OpenAPI spec (this folder)
├── serverless.yml     # SF service: functions, HTTP API, schedules, wiring
├── infrastructure/    # All Terraform: stateful/shared/secret resources
├── src/               # Python Lambda source (packaged by SF)
│   ├── api/           # Public API handlers
│   ├── authorizer/    # Lambda authorizer
│   ├── ingest/        # Scheduled + on-demand ingestion workers
│   ├── adapters/      # One module per upstream source
│   ├── synthetic/     # Deterministic data generator
│   └── shared/        # Models, DynamoDB repo layer, config
├── cli/               # Operator CLI (key issuance, backfill triggers)
├── scripts/           # One-time admin bootstrap and developer utilities
└── tests/
```
