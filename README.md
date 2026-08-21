# Fauxnance API

A free, serverless market-data API built for bank graduate-training capstone projects
(trading platforms, portfolio managers, asset managers). It aggregates data from free
upstream sources, stores it in DynamoDB, and serves it behind per-student API keys —
so trainees stop fighting paid data vendors and start building.

**Status: Phase 4 is implemented and deployed to AWS.** The repository includes
the authenticated HTTP API, API-key quotas, 515 US symbols, 30 Indian equities,
12 FX pairs, 12 cryptocurrencies, resumable and lazy historical backfills,
corporate actions, cache-first quotes, deterministic synthetic fallback, and
the admin API and CLI for cohort/key operations. Four market schedules feed
the guarded multi-source ingest pipeline through SQS with retries and a DLQ.
The initial India, FX, and crypto historical backfills completed on
2026-07-22. The API uses API Gateway's generated URL:
`https://y4t9nq2bqf.execute-api.eu-west-2.amazonaws.com`.

**New, not yet deployed:** a session-authenticated instructor console (web
frontend + `console` Lambda) that lets admins and instructors manage classes
and students without the CLI. Code and tests are complete; see
[08-console](docs/08-console.md).

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
| [08-console](docs/08-console.md) | Instructor console: auth, routes, and frontend deployment |

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

Alpha Vantage, Finnhub, and CoinGecko Demo are optional keyed sources. Enable
them with SSM `SecureString` values at
`/fauxnance/dev/upstreams/{alpha_vantage|finnhub|coingecko}/api_key`. Missing
parameters leave the no-key Yahoo and Frankfurter paths operational.
