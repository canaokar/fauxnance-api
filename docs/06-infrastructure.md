# 06 — Infrastructure

Phase 1 keeps infrastructure deliberately small. Terraform owns the persistent
data and authentication configuration; Serverless Framework owns the HTTP API
and Lambda functions.

## Phase 1 resources

| Resource | Owner |
|---|---|
| `fauxnance-dev-data` DynamoDB table | Terraform |
| `fauxnance-dev-control` DynamoDB table | Terraform |
| `/fauxnance/dev/admin/key_ids` SSM parameter | Terraform |
| HTTP API, API Lambda, authorizer Lambda, IAM roles, and logs | Serverless Framework |

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
Terraform state is sufficient for the single Phase 1 operator and is ignored by
Git.

## Deployment

```sh
make infra-plan
make infra
npx serverless login
.venv/bin/python scripts/bootstrap_admin.py --seed-dev-student
.venv/bin/python scripts/backfill_dev.py
make deploy
```

The bootstrap script replaces the placeholder admin allowlist value. Terraform
ignores later changes to that value so subsequent applies do not revoke keys.

## Deferred

Remote state, reusable Terraform modules, multiple environment roots, SQS/DLQ,
scheduled ingestion, upstream API-key parameters, alarms, dashboards, and
budgets are added only in the phase that uses them.
