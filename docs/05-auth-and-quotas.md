# 05 — Auth, Keys & Quotas

## Key format & storage

- Format: `fnx_<env>_<32 chars base62>` — e.g. `fnx_live_9xK...`, `fnx_dev_...`.
  The prefix makes keys grep-able in student code reviews and secret scanners.
- **Only the SHA-256 hash is stored** (`KEY#<hash>` in the control table).
  Plaintext is shown exactly once, in the `POST /v1/admin/keys` response / CLI
  output. Lost key → revoke + reissue (30 seconds of instructor time; also a
  teachable moment).
- Key types: `student` and `admin`. Admin keys additionally must appear in an
  SSM-stored keyId allowlist — a second factor so a leaked control-table dump
  can't mint admin access.

## Cohort model

```
Cohort (e.g. "HDFC-GradBatch-2026Q3")
 ├─ defaultDailyQuota   (applied to keys at issue time; per-key override allowed)
 ├─ expiresAt           (hard stop — all cohort keys 403 after this date)
 └─ Keys (one per student, labeled: "amara-okafor", "team-3", ...)
```

- Cohorts map to training batches. Expiry means never having to remember to
  clean up after a program ends; storage TTL reaps usage counters automatically.
- Aggregate usage per cohort is derivable from usage items (admin endpoint sums
  them) — good enough at this scale, no pre-aggregation.

## Request lifecycle

1. **Authorizer** (300 s result cache): hash presented key → read the key item;
   for a student key, also read its cohort and deny if either is inactive or
   expired; for an admin key, verify its `keyId` is in the SSM allowlist. On
   success pass `{keyId, cohortId?, keyType, dailyQuota?}` to the handler. The
   hash is not passed; usage counters are keyed by `keyId`.
2. **Quota check** in the API Lambda: conditional atomic increment of today's
   usage item (see [03-data-model](03-data-model.md)); failure → `429` with
   `Retry-After` = seconds to 00:00 UTC.
3. Revocation latency = authorizer cache TTL (≤ 300 s). Acceptable; documented.

Every authenticated student request that reaches the public API handler counts,
including validation failures and `202` responses. The unauthenticated health
endpoint and all admin requests do not consume a student quota. Calling
`GET /v1/usage` does count, so its `usedToday` includes that request.

## Limits (defaults; configured per stage in `serverless.yml`)

| Limit | Default | Enforced by |
|---|---|---|
| Per-key daily quota | 2,000 req/day | DynamoDB conditional counter |
| Batch quote size | 25 symbols (counts as 1 request) | Handler validation |
| Stage-wide burst | 50 rps / 25 burst | API Gateway stage throttling — blunt backstop protecting the AWS bill from a runaway loop, sized so one misbehaving script can't starve a class |
| Payload size | Gateway defaults | — |

Why not API Gateway usage plans: they require REST API (3× cost), can't group by
cohort, and can't be revoked/queried from a CLI as flexibly as DynamoDB items.

### Why not Cognito in v1

Cognito user pools solve interactive user sign-in and issue expiring JWTs. The
v1 clients are scripts, curl, Excel, and student applications that need a stable
instructor-issued credential, not a login/session flow. Cognito would make each
client obtain, store, and refresh tokens while Fauxnance would still need its
own DynamoDB model for cohorts, daily quotas, labels, and usage reporting.

Keep the Lambda authorizer for v1. Reconsider Cognito only if self-serve signup,
password recovery, MFA, or federation with a bank identity provider becomes a
real requirement; that is an authentication-product scope change, not an
implementation detail.

## One-time admin bootstrap

The admin API cannot create admin keys, avoiding a circular trust path. Phase 1
includes `scripts/bootstrap_admin.py`, the one deliberate exception to the
API-only operator rule. With operator AWS credentials it:

1. generates an admin key and prints it once;
2. transactionally writes its hashed key item and `KEYID#<keyId>` lookup to the
   control table; and
3. adds the `keyId` to the stage's SSM admin allowlist.

For the Phase 1 walking skeleton, `--seed-dev-student` also creates one dev cohort
and student key so the auth/quota path can be tested before the full admin API is
built. It is safe to rerun with a new label/keyId. Normal cohort and student-key
work then goes exclusively through the admin API and CLI.

## Operator CLI (`cli/`)

Thin Python client over the admin API (never touches AWS directly — it dogfoods
the API; only prerequisite is an admin key in the environment):

```
fnx cohorts create "HDFC-GradBatch-2026Q3" --quota 2000 --expires 2026-12-31
fnx keys issue --cohort <id> --labels-file students.txt   # one key per line, CSV out
fnx keys revoke <keyId>
fnx keys list --cohort <id>
fnx backfill --universe sp500 --from 2016-01-01
fnx jobs status <jobId>
```

The CSV output (`label,key`) is what gets mail-merged to students on day one.

## Security posture

- **Threat model is deliberately modest:** the asset is the operator's AWS bill
  and upstream quotas, not the data (which is free elsewhere). No PII beyond key
  labels the instructor chooses (told: use handles, not full names+emails).
- HTTPS only (Gateway default). No CORS restrictions — students call from
  localhost and random sandboxes; keys, not origins, are the control. The HTTP
  API CORS configuration explicitly allows `*` origins, required methods, and
  the `X-Api-Key` and `Content-Type` request headers; credentials are disabled.
- IAM remains per-function and least-privilege:
  - `api`: read market data; write quote-cache and conditional on-demand
    `META`/`SEARCH` items; update usage/source-state items; send backfill messages;
    read only required upstream-key parameters.
  - `authorizer`: read key/cohort items and the admin allowlist parameter.
  - `dispatcher`: read registry projections and send ingest messages.
  - `ingest-worker`: read/write market data and job/source-state items; read
    required upstream-key parameters.
  - `admin`: manage cohort/key/job/registry items and send backfill messages.
- Upstream keys in SSM `SecureString`, fetched at cold start, cached for the
  Lambda lifetime, never logged.
- Structured logs redact `X-Api-Key` and anything matching `fnx_[a-z]+_\w+`.
- CloudWatch alarms: authorizer 5xx, sustained 429 spikes (misconfigured student
  loop), DLQ depth > 0, and an AWS Budget alert at $5/month as the final tripwire.
