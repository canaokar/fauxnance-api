# 06 — Infrastructure

Infrastructure remains deliberately small. Terraform owns persistent data and
authentication configuration. Serverless Framework owns the HTTP API, Lambda
functions, schedule, and transient ingest messaging.

## Resources

| Resource | Owner |
|---|---|
| `fauxnance-dev-data` DynamoDB table | Terraform |
| `fauxnance-dev-control` DynamoDB table | Terraform |
| `/fauxnance/dev/admin/key_ids` SSM parameter | Terraform |
| HTTP API, four Lambdas, four EventBridge rules, IAM roles, and logs | Serverless Framework |
| Ingest queue, DLQ, redrive policy, and DLQ-depth alarm | Serverless Framework |

The table names are fixed for the dev stage and are used directly by both tools.
There is no output handoff layer.

## Layout

```text
infrastructure/
├── main.tf
├── .terraform.lock.hcl
├── .gitignore
└── README.md
```

`main.tf` contains the requested `megh.io` provider configuration for
`eu-west-2`, the two DynamoDB tables, and the admin allowlist parameter. Local
Terraform state is sufficient for the single dev operator and is ignored by Git.

## Deployment

```sh
make infra-plan
make infra
npx serverless login
.venv/bin/python scripts/bootstrap_admin.py --seed-dev-student
make deploy
.venv/bin/python scripts/enqueue_backfill.py
```

The bootstrap script replaces the placeholder admin allowlist value. Terraform
ignores later changes to that value so subsequent applies do not revoke keys.
The dev API is currently served directly from
`https://y4t9nq2bqf.execute-api.eu-west-2.amazonaws.com`; no custom domain or
API mapping is configured.

## Deferred

Remote state, reusable Terraform modules, multiple environment roots, custom
domains, SNS notifications, dashboards, and budgets remain deferred. The
optional Alpha Vantage key is written directly to SSM and never Terraform state.
Finnhub and CoinGecko Demo keys follow the same optional SecureString pattern.
