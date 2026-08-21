# Fauxnance operations runbook

This runbook covers the single deployed `dev` environment. The service is
`fauxnance-api`, the CloudFormation stack is `fauxnance-api-dev`, and the public
base URL is
`https://y4t9nq2bqf.execute-api.eu-west-2.amazonaws.com/v1`. There is no custom
domain. All times and daily quotas use UTC.

## First response

1. Check liveness and market freshness. A `200` with `status: degraded` means
   the API is live but at least one market is stale.

   ```sh
   curl --fail-with-body https://y4t9nq2bqf.execute-api.eu-west-2.amazonaws.com/v1/health
   ```

2. Open the `fauxnance-api-dev-operations` CloudWatch dashboard and set its
   range to cover at least 30 minutes before the alarm.

   ```sh
   aws cloudwatch get-dashboard --dashboard-name fauxnance-api-dev-operations --profile=megh.io --region eu-west-2
   aws cloudwatch describe-alarms --alarm-name-prefix fauxnance-api-dev --profile=megh.io --region eu-west-2
   ```

3. Confirm that the stack is stable before changing anything.

   ```sh
   aws cloudformation describe-stacks --stack-name fauxnance-api-dev --query 'Stacks[0].StackStatus' --output text --profile=megh.io --region eu-west-2
   ```

## Reading the dashboard

| Widget | Interpretation |
|---|---|
| API `Count`, `4xx`, `5xx` | `Count` is demand. Occasional `4xx` is expected from missing keys, validation, and quotas; correlate a sustained rise with route/status access logs. Any `5xx` needs investigation. |
| API `Latency`, `IntegrationLatency` | Compare p95 values. Both rising points to Lambda or its dependencies; high `Latency` with normal integration latency points to API Gateway/front-door overhead. Compare against the API Lambda's 15-second timeout. |
| Lambda `Errors`, `Throttles`, `Duration` | Errors in `api`, `admin`, `console`, `authorizer`, `dispatcher`, or `backfillCoordinator` are control-path failures. Throttles mean concurrency pressure. p95 duration approaching a function timeout predicts failures. The ingest worker reports per-record failures through SQS, so queue age and DLQ depth are more authoritative than its Lambda `Errors`. |
| SQS visible, in-flight, oldest age | Visible is backlog; in-flight is work held by the two ingest workers. A short burst after a schedule or backfill is normal. Rising visible count and oldest age with flat in-flight count suggests a disabled or failing event-source mapping. |
| SQS DLQ | Any message in `fauxnance-api-dev-ingest-dlq` needs action. This is the service's only DLQ. |
| DynamoDB consumption and throttles | Compare consumed capacity with the provisioned capacity for `fauxnance-dev-data` and `fauxnance-dev-control`. Any `ReadThrottleEvents` or `WriteThrottleEvents` during normal cohort traffic warrants investigation; correlate with Lambda duration and API 5xx. |

HTTP access logs are in `/aws/http-api/fauxnance-api-dev`. Lambda logs are in
`/aws/lambda/fauxnance-api-dev-{api,admin,console,authorizer,dispatcher,backfillCoordinator,ingestWorker}`
with 14-day retention.

## Alarm triage

### API 5xx

For `fauxnance-api-dev-api-5xx`, find affected request IDs, then correlate them
with `api` or `admin` logs. Do not search for or paste `X-Api-Key` values.

```sh
aws logs filter-log-events --log-group-name /aws/http-api/fauxnance-api-dev --start-time 0 --filter-pattern '{ $.status = %5[0-9][0-9]% }' --limit 50 --profile=megh.io --region eu-west-2
aws logs tail /aws/lambda/fauxnance-api-dev-api --since 30m --format short --profile=megh.io --region eu-west-2
aws logs tail /aws/lambda/fauxnance-api-dev-admin --since 30m --format short --profile=megh.io --region eu-west-2
```

Check DynamoDB throttles and upstream timeouts before rollback. A public
dependency failure should normally produce a documented `503`, not a `500`.

### Authorizer errors

For `fauxnance-api-dev-authorizer-errors`, distinguish a Lambda error from a
normal `401`/`403`. Normal denials do not increment Lambda `Errors`. Check the
control table and admin allowlist parameter metadata, never its value.

```sh
aws logs tail /aws/lambda/fauxnance-api-dev-authorizer --since 30m --format short --profile=megh.io --region eu-west-2
aws dynamodb describe-table --table-name fauxnance-dev-control --profile=megh.io --region eu-west-2
aws ssm describe-parameters --parameter-filters Key=Name,Option=Equals,Values=/fauxnance/dev/admin/key_ids --query 'Parameters[].{Name:Name,Type:Type,LastModifiedDate:LastModifiedDate}' --profile=megh.io --region eu-west-2
```

### Dispatcher or coordinator errors

For `fauxnance-api-dev-dispatcher-errors`, inspect the dispatcher log and the
four EventBridge rules. Do not manually invoke it until duplicate queued work is
understood.

```sh
aws logs tail /aws/lambda/fauxnance-api-dev-dispatcher --since 2h --format short --profile=megh.io --region eu-west-2
aws events list-rules --name-prefix fauxnance-api-dev --profile=megh.io --region eu-west-2
```

For `fauxnance-api-dev-backfill-coordinator-errors`, inspect the coordinator and
admin logs. A retryable job may remain `preparing`, or `running` with
`enqueueComplete` false, until lease recovery reclaims it.

```sh
aws logs tail /aws/lambda/fauxnance-api-dev-backfillCoordinator --since 2h --format short --profile=megh.io --region eu-west-2
aws logs tail /aws/lambda/fauxnance-api-dev-admin --since 2h --format short --profile=megh.io --region eu-west-2
```

Use the redacted admin API for diagnosis:

```sh
fnx jobs status JOB_ID
fnx cohorts list
fnx keys list --cohort COHORT_ID
```

These responses expose IDs, labels, status, usage, and bounded failure details;
they must never expose plaintext keys or key hashes. Do not use DynamoDB scans
or print `KEY#...` records during routine diagnosis.

### Ingest queue age or DLQ

For `fauxnance-api-dev-ingest-queue-age`, first determine whether the queue is
draining and whether the event-source mapping remains enabled.

```sh
aws sqs get-queue-attributes --queue-url https://sqs.eu-west-2.amazonaws.com/211125506432/fauxnance-api-dev-ingest --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible RedrivePolicy --profile=megh.io --region eu-west-2
aws lambda list-event-source-mappings --function-name fauxnance-api-dev-ingestWorker --query 'EventSourceMappings[].{State:State,LastProcessingResult:LastProcessingResult,BatchSize:BatchSize,ScalingConfig:ScalingConfig}' --profile=megh.io --region eu-west-2
aws logs tail /aws/lambda/fauxnance-api-dev-ingestWorker --since 30m --format short --profile=megh.io --region eu-west-2
```

The `fauxnance-api-dev-ingest-dlq-not-empty` alarm means retries are exhausted.
Inspect one message without deleting it or extending its invisibility. Ingest
bodies contain job IDs, symbols, and dates, not credentials; still do not paste
them into tickets or chat.

```sh
aws sqs receive-message --queue-url https://sqs.eu-west-2.amazonaws.com/211125506432/fauxnance-api-dev-ingest-dlq --max-number-of-messages 1 --visibility-timeout 0 --attribute-names All --query 'Messages[].{MessageId:MessageId,Attributes:Attributes,Body:Body}' --profile=megh.io --region eu-west-2
```

### Redrive decision

Redrive is a state-changing operator action. Do it only after all of the
following are true:

- the root cause is fixed or the upstream has recovered;
- the message's job/key/symbol scope is understood;
- replay is idempotent for that message version;
- current queue depth and DynamoDB capacity can absorb the replay; and
- the operator explicitly records a **redrive approved** decision.

After approval, move the ingest DLQ back to its configured source queue. Start
with a low rate and save the returned task handle.

```sh
aws sqs start-message-move-task --source-arn arn:aws:sqs:eu-west-2:211125506432:fauxnance-api-dev-ingest-dlq --max-number-of-messages-per-second 1 --profile=megh.io --region eu-west-2
aws sqs list-message-move-tasks --source-arn arn:aws:sqs:eu-west-2:211125506432:fauxnance-api-dev-ingest-dlq --profile=megh.io --region eu-west-2
```

## Rollback and deployment verification

Prefer a Git revert of the faulty commit so the rollback is auditable, then
deploy the resulting `main`. Serverless function versions are disabled, so do
not rely on an unpublished Lambda version rollback.

```sh
npx serverless deploy --stage dev --region eu-west-2 --aws-profile megh.io
aws cloudformation wait stack-update-complete --stack-name fauxnance-api-dev --profile=megh.io --region eu-west-2
aws cloudformation describe-stacks --stack-name fauxnance-api-dev --query 'Stacks[0].StackStatus' --output text --profile=megh.io --region eu-west-2
aws cloudwatch describe-alarms --alarm-name-prefix fauxnance-api-dev --query 'MetricAlarms[].{Name:AlarmName,State:StateValue,Reason:StateReason}' --profile=megh.io --region eu-west-2
```

Then call `/v1/health`, confirm all expected HTTP routes and functions with
`npx serverless info --stage dev --region eu-west-2 --aws-profile megh.io`, and
check that the ingest queue is draining and `fauxnance-api-dev-ingest-dlq` is
empty.

## Pilot and first cohort

Before issuing real credentials, run the destructive pilot with an allowlisted
admin key in `FNX_API_KEY`. The output path must not exist; the file is created
with mode `0600`. The script issues exactly 50 temporary keys, performs sampled
authenticated usage calls, revokes every key, and inactivates the cohort in its
cleanup path.

```sh
.venv/bin/python scripts/pilot_dry_run.py --execute --output /tmp/fauxnance-phase4-pilot.csv --max-seconds 600
```

The JSON summary must report `success: true`, `keysIssued: 50`,
`smokePassed: 3`, `cleanupErrors: 0`, and issuance under 600 seconds. Delete the
temporary CSV after reviewing the result because its credentials are revoked.

### Before day one

- Confirm `/v1/health` is `ok` for US, IN, FX, and CRYPTO.
- Confirm every `fauxnance-api-dev-*` alarm is `OK`, the ingest DLQ is empty,
  and the ingest queue has no unexplained old messages.
- Complete the 50-key pilot and retain its non-secret summary.
- Create the real cohort with the intended expiry/quota; issue the CSV once to
  an exclusive `0600` file; verify its row count before distribution.
- Smoke-test one real key against `/v1/usage`, `/v1/quotes/AAPL`, and a short
  `/v1/candles/AAPL` range without displaying the key.

### Week one

- Check health, alarms, queue age/DLQ, API 5xx, p95 latency, Lambda throttles,
  and DynamoDB throttles after each scheduled ingest and at class start.
- Treat a 4xx rise as a student-support signal; distinguish 401/403, validation,
  and intentional 429 quota responses before changing limits.
- Use `fnx cohorts list`, `fnx keys list --cohort COHORT_ID`, and
  `fnx jobs status JOB_ID` for routine diagnosis. Revoke lost keys by key ID;
  never request, store, or log their plaintext again.
- Record incidents, redrive decisions, and operator actions. At week end,
  confirm cohort usage, ingestion freshness, and that no job remains stuck in
  `preparing` or incomplete `running` state.

This phase deliberately has no AWS Budget resource or custom-domain procedure.
