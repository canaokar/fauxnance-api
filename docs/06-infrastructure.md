# 06 — Infrastructure (Terraform + Serverless Framework hybrid)

The infrastructure is split between two tools with a **hard ownership rule**:

> **Terraform owns everything stateful, shared, or secret.
> Serverless Framework owns everything stateless-compute and its wiring.
> No resource is ever defined in both.**

This mirrors how many bank platform teams actually operate (platform owns
Terraform-managed shared infra; app teams own their `serverless.yml`), which
makes the split itself a teaching artifact. It also plays to each tool's
strength: SF's sweet spot is exactly Lambda + API Gateway; Terraform covers what
SF/CloudFormation handles poorly or not at all (notably SSM `SecureString`,
which CloudFormation cannot create).

## Ownership map

| Resource | Owner | Why |
|---|---|---|
| DynamoDB tables (`data`, `control`), TTL, capacity | **Terraform** | Stateful — must survive `sls remove`; capacity tuning is TF-variable-driven |
| SQS ingest queue + DLQ | **Terraform** | Shared, holds in-flight data; SF only *consumes* its ARN |
| SSM parameters (secrets + config + handoff) | **Terraform** | CFN can't create `SecureString`; values set out-of-band |
| DynamoDB/SQS alarms and dashboard | **Terraform** | Monitor Terraform-owned/shared resources; dashboard uses stable names only |
| API Gateway/Lambda alarms | **Serverless Framework** | They require IDs/resources created in the same SF stack and avoid a reverse handoff |
| AWS Budget alert ($5) | **Terraform** | Raw CFN in SF; trivial in TF |
| Custom domain | **Deferred to v1.x** | v1 uses the raw HTTP API URL; domain ownership/mapping will be specified when needed |
| Remote-state S3 bucket | **Terraform** (bootstrap) | TF's own plumbing |
| Lambda functions ×5 (`api`, `authorizer`, `dispatcher`, `ingest-worker`, `admin`) + packaging | **Serverless Framework** | Its core competency: builds, zips, deploys `src/` |
| Per-function IAM roles | **Serverless Framework** | Kept next to the functions they scope (`serverless-iam-roles-per-function`) |
| HTTP API, routes, stage, throttling, Lambda authorizer wiring | **Serverless Framework** | Native `httpApi` events |
| EventBridge cron schedules | **Serverless Framework** | Native `schedule` events on the dispatcher function |
| SQS → worker event-source mapping | **Serverless Framework** | Function wiring (queue ARN read from handoff params) |
| CloudWatch log groups + retention | **Serverless Framework** | Created alongside their functions |

**Rule of thumb for new resources:** if deleting it would lose data, break another
component, or leak a secret → Terraform. If it only executes code → SF.

## The handoff contract

Terraform → SF communication happens **only** through plain SSM parameters that
Terraform writes under a reserved prefix:

```
/fauxnance/<env>/infra/data_table_name
/fauxnance/<env>/infra/control_table_name
/fauxnance/<env>/infra/ingest_queue_arn
/fauxnance/<env>/infra/ingest_queue_url
/fauxnance/<env>/infra/dlq_arn
/fauxnance/<env>/infra/region
```

`serverless.yml` consumes them natively:

```yaml
provider:
  environment:
    DATA_TABLE: ${ssm:/fauxnance/${sls:stage}/infra/data_table_name}
functions:
  ingestWorker:
    events:
      - sqs:
          arn: ${ssm:/fauxnance/${sls:stage}/infra/ingest_queue_arn}
```

No hardcoded ARNs, no cross-referencing state files, and the contract is
greppable in one place. SF never *writes* under `/infra/`; Terraform never reads
SF-owned resources. Alarms that require an HTTP API ID therefore live in the SF
stack.

## Layout

```
fauxnance-api/
├── serverless.yml           # SF service: functions, httpApi, schedules, wiring
├── infrastructure/          # All Terraform
│   ├── bootstrap/           # One-time remote-state bucket setup (local state)
│   ├── modules/
│   │   ├── data/            # DynamoDB tables, TTL, capacity vars
│   │   ├── ingestion/       # SQS + DLQ
│   │   ├── secrets/         # SSM parameter definitions (see below)
│   │   ├── handoff/         # /infra/* output parameters
│   │   └── observability/   # Stateful-resource alarms, dashboard, budget alert
│   ├── environments/
│   │   ├── dev/             # main.tf + backend.tf + stage variables
│   │   └── prod/            # main.tf + backend.tf + stage variables
└── src/                     # Python, packaged by SF
```

- **Environments:** TF uses `environments/<env>/` directories; SF uses
  `--stage <env>`. The names must match (`dev`, `prod`) — the SSM prefix is the
  join key.
- **Region/account:** Terraform and Serverless use `eu-west-2` and the `megh.io`
  shared AWS profile. Terraform still publishes the region through the `/infra/`
  handoff contract for runtime configuration and diagnostics.

## Deploy order & workflow

Terraform first (SF resolves `${ssm:}` at deploy time), SF second. One Makefile
fronts both:

```
make bootstrap          # first deployment only: create remote-state bucket
make infra ENV=dev     # terraform -chdir=infrastructure/environments/dev apply
make deploy ENV=dev    # sls deploy --stage dev   (implies: make infra first run)
make destroy ENV=dev   # sls remove --stage dev, THEN terraform destroy (reverse order)
make logs FN=api ENV=dev
```

Day-to-day code changes touch only `sls deploy` (~1 min); Terraform runs only
when stateful infra changes. No CI/CD in v1 — single operator; a GitHub Actions
pipeline (TF plan/apply + `sls deploy`) is a Phase 4 nice-to-have.

## Secrets & configuration

| Item | Where | Notes |
|---|---|---|
| Upstream API keys (Finnhub, Alpha Vantage, CoinGecko Demo) | SSM `SecureString` `/fauxnance/<env>/sources/<name>/api_key` | Created by TF with placeholder + `lifecycle { ignore_changes = [value] }`; **values set manually** via `aws ssm put-parameter`, never in TF state or serverless.yml |
| Admin keyId allowlist | SSM String `/fauxnance/<env>/admin/key_ids` | Comma-separated |
| Infra handoff values | SSM String `/fauxnance/<env>/infra/*` | Written by TF, read by SF (above) |
| Tunables (TTLs, quotas, budgets, universe caps) | `serverless.yml` env vars per stage | Change = `sls deploy`; fine for an instructor-operated service |

## Serverless Framework specifics

- Serverless Framework **v4.39.0** is pinned in `package-lock.json`. V3's final
  runtime schema predates Lambda Python 3.13, so it cannot validate this service.
  V4 is free below $2 M organization revenue but requires a one-time Serverless
  account login before packaging/deployment.
- Plugins: exactly `serverless-iam-roles-per-function` and
  `serverless-python-requirements`. New plugins need a spec change.
- Packaging: SF's `package.patterns` per function, with dependencies vendored by
  `serverless-python-requirements`; arm64, no pandas/numpy, zips < 10 MB.
- HTTP API CORS is explicit: allow origin `*`, methods used by the public/admin
  API, and request headers `X-Api-Key` and `Content-Type`; do not enable
  credentials. API Gateway handles unauthenticated preflight requests.
- `sls remove` must be non-catastrophic at any time: it can only ever delete
  compute. That property is the ownership rule, enforced.

## Cost model (monthly, steady state, 1 active cohort of 50)

Unchanged by the hybrid — same resources, different authors:

| Service | Usage estimate | Cost |
|---|---|---|
| API Gateway (HTTP) | < 1 M requests | $0 (free tier yr 1) → ~$1 after |
| Lambda | Well under always-free 400k GB-s + 1 M reqs | $0 |
| DynamoDB | Provisioned within always-free 25/25 RCU/WCU; ~150 MB | $0 |
| SQS / EventBridge / SSM standard | Within always-free tiers | $0 |
| CloudWatch | ~1 GB logs, short retention, few alarms | ~$0.50 |
| Route 53 hosted zone | Deferred with the custom domain | $0 in v1 |
| **Total** | | **≈ $0.50–2.00** |

Blast-radius protections unchanged: stage throttling (SF), per-key quotas
(DynamoDB), ingest reserved concurrency = 2 (SF), Budget alarm at $5 (TF).

## Tagging & hygiene

- Every resource, both tools: `project=fauxnance`, `env=<env>`, plus
  `managed-by=terraform` or `managed-by=serverless` — so any resource's owner is
  answerable from the console in one glance.
- `terraform fmt`/`validate`/`tflint` and `sls package --stage dev` (as a
  validity check) in pre-commit. TF state in S3 with versioning.
