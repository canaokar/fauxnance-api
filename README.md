# Fauxnance API

A free, serverless market-data API built for bank graduate-training capstone projects
(trading platforms, portfolio managers, asset managers). It aggregates data from free
upstream sources, stores it in DynamoDB, and serves it behind per-student API keys —
so trainees stop fighting paid data vendors and start building.

**Status: specification phase.** No code yet. All design documents live in [`docs/`](docs/).

| Doc | Contents |
|---|---|
| [00-overview](docs/00-overview.md) | Vision, goals, non-goals, key decisions |
| [01-architecture](docs/01-architecture.md) | AWS components and request/ingest flows |
| [02-api-spec](docs/02-api-spec.md) | Endpoints, symbol scheme, response envelope, errors |
| [03-data-model](docs/03-data-model.md) | DynamoDB table design and access patterns |
| [04-ingestion](docs/04-ingestion.md) | Source adapters, schedules, backfill, synthetic fallback |
| [05-auth-and-quotas](docs/05-auth-and-quotas.md) | API keys, cohorts, rate limiting, admin API |
| [06-infrastructure](docs/06-infrastructure.md) | Terraform layout, environments, SSM, cost model |
| [07-roadmap](docs/07-roadmap.md) | Build phases and future simulation module |
