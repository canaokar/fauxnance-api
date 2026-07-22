# Error-handling guide

Branch on HTTP status first. Fauxnance Lambda errors normally use this envelope:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "The request is invalid.",
    "details": {}
  }
}
```

API Gateway can reject a request before a Lambda runs, so `401`, some `403`,
and gateway-throttling responses are not guaranteed to use that shape. Parse
the body only after checking its content type and structure. Never include the
`X-Api-Key` header in logs or exception messages.

| HTTP | Meaning | Client action |
|---|---|---|
| `202` | Data or an admin backfill is still being prepared | Read `Retry-After` when present, wait, then poll the supplied resource. |
| `400` | Invalid field, symbol, date, cursor, interval, or range | Fix the request. Retrying the same request will not help. |
| `401` | Missing, malformed, unknown, or revoked key | Check that `X-Api-Key` is set. Replace a lost key; do not print it while debugging. |
| `403` | Inactive/expired identity, or `ADMIN_ONLY` | Students should contact the instructor. Operators should verify they used an allowlisted admin key. |
| `404` | Route, symbol, cohort, key, universe, or job does not exist | Correct the identifier. Do not retry unless another operation is creating it asynchronously. |
| `409` | Request conflicts with current state | Refresh the resource and resolve the conflict; do not blindly retry. |
| `429` | Daily quota or gateway throttle reached | Respect `Retry-After`. Batch quote reads and reduce polling. |
| `500` | Unexpected Fauxnance failure | Retry a small number of times with exponential backoff and jitter, then report the request time and route. |
| `502`–`504` | Gateway or transient dependency failure | Retry as for `500`; check `/v1/health` if failures persist. |

`503 UPSTREAM_UNAVAILABLE` is normally temporary. Wait at least the supplied
`Retry-After` value, then retry with exponential backoff. Stored, stale, or
synthetic data may still be returned as a successful response during an
upstream outage.

## Retry pattern

Only retry `429`, `500`, `502`, `503`, and `504`, plus explicitly asynchronous
`202` responses. Cap attempts and add random jitter so a class does not retry in
lockstep. A reasonable teaching-client sequence is approximately 1, 2, 4, and
8 seconds, while always preferring a longer `Retry-After` value from the server.

Do not automatically retry key-issuance `POST` requests after an ambiguous
network failure: plaintext keys are returned once, and an automatic replay can
issue duplicates. Check the cohort's key list and reconcile labels first.

## Batch quote errors

`GET /v1/quotes?symbols=...` can return HTTP `200` with a mixture of successes
and errors. Each entry contains exactly one of `quote` or `error`; handle every
item independently.

## Useful diagnostics

- `GET /v1/health` is unauthenticated and distinguishes service-wide freshness
  problems from credential or client problems.
- `GET /v1/usage` shows today's allowance and reset time, but consumes one quota
  unit itself.
- Record the UTC request time, HTTP status, route, and non-secret error code when
  asking for help. Never record request headers containing credentials.
