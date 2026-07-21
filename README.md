# Fauxnance API

A free, serverless market-data API built for bank graduate-training capstone projects
(trading platforms, portfolio managers, asset managers). It aggregates data from free
upstream sources, stores it in DynamoDB, and serves it behind per-student API keys —
so trainees stop fighting paid data vendors and start building.

**Status: Phase 1 walking skeleton implemented.** The repository includes the
Terraform foundation, Serverless HTTP API, cached API-key authorizer, daily
quotas, US EOD endpoints, Yahoo adapter, bootstrap/backfill utilities, and the
[OpenAPI contract](docs/openapi.yaml). AWS deployment is an operator action.

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

## Local verification

Requires Python 3.13+, Node.js, Terraform, and the `megh.io` AWS profile for
deployment. Tests do not call AWS or market-data providers.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
npm ci
.venv/bin/python -m unittest discover -s tests -v
```

## Phase 1 deployment order

Choose a globally unique Terraform state bucket name and run each step from the
repository root. Terraform is intentionally separate from Serverless deployment.

```sh
make bootstrap STATE_BUCKET=<globally-unique-bucket-name>
make infra STATE_BUCKET=<globally-unique-bucket-name>
npx serverless login
.venv/bin/python scripts/bootstrap_admin.py --seed-dev-student
.venv/bin/python scripts/backfill_dev.py
make deploy
```

The bootstrap script prints the admin and optional dev-student keys once. Store
them securely; only their SHA-256 hashes are written to DynamoDB.
