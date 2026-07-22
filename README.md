# Fauxnance API

A free, serverless market-data API built for bank graduate-training capstone projects
(trading platforms, portfolio managers, asset managers). It aggregates data from free
upstream sources, stores it in DynamoDB, and serves it behind per-student API keys —
so trainees stop fighting paid data vendors and start building.

**Status: Phase 2 is implemented and deployed to AWS; the initial historical
backfill completed on 2026-07-22; only the one-week schedule observation
remains.** The repository includes the HTTP API, API-key quotas, a 515-symbol
US universe, resumable historical
backfills, and scheduled Yahoo-to-Alpha EOD ingestion with SQS retries and a
DLQ. The API uses API Gateway's generated URL:
`https://y4t9nq2bqf.execute-api.eu-west-2.amazonaws.com`.

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

## Deployment and initial backfill

Run each step from the repository root. Terraform is intentionally separate
from Serverless deployment.

```sh
make infra-plan
make infra
npx serverless login
.venv/bin/python scripts/bootstrap_admin.py --seed-dev-student
make deploy
.venv/bin/python scripts/enqueue_backfill.py
```

The bootstrap script prints the admin and optional dev-student keys once. Store
them securely; only their SHA-256 hashes are written to DynamoDB. The backfill
command prints its job ID; rerun it with `--job-id <id>` and the original year
bounds to re-enqueue only unfinished work.

`make deploy` prints the default API Gateway endpoint. It can also be retrieved
later with `npx serverless info --stage dev --region eu-west-2 --aws-profile megh.io`.
No custom domain is configured.

Alpha Vantage is an optional emergency fallback. To enable it, create
`/fauxnance/dev/upstreams/alpha_vantage/api_key` as an SSM `SecureString`; a
missing parameter leaves the primary Yahoo path operational.
