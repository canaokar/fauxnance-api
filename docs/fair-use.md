# Fair-use policy

Fauxnance is a shared educational service. The default student allowance is
2,000 authenticated requests per UTC day, although an instructor can choose a
different quota for a cohort or key. API Gateway also applies service-wide
throttling to protect every class from accidental loops.

## Use the service efficiently

- Request up to 25 symbols with the batch quote endpoint; the batch consumes one
  quota unit.
- Cache responses in your application. Quotes have an application freshness
  window of about five minutes, so faster polling rarely adds useful information.
- Fetch daily candles after the relevant market close, not every few seconds.
  Reuse a stored historical response until the next trading day.
- Poll asynchronous backfill jobs no more frequently than every 30 seconds and
  respect every `Retry-After` header.
- Stop retrying after a small number of attempts. Use exponential backoff with
  jitter for `429` and transient `5xx` responses.
- Check `/v1/health` before a class-wide retry storm.

## Protect credentials

Each key identifies a student or team. Do not commit it to Git, hard-code it in
a public browser bundle, paste it into support messages, or share it across
teams. Use environment variables for scripts. Remove Power Query key parameters
before sharing Excel workbooks. Report a leaked key promptly so the instructor
can revoke and replace it.

## Understand the data

The API is for training and demonstration only, not investment decisions,
execution, valuation, regulatory reporting, or production systems. Data can be
delayed, stale, incomplete, synthetic, or sourced from free third parties.

Read response metadata:

- `meta.source` explains whether data was stored, cached, synthetic, mixed, or
  fetched from an upstream.
- `meta.stale: true` marks a quote served beyond its normal freshness window.
- Candle rows with `synthetic: true` are deterministic teaching data, not market
  observations.
- `meta.partial` and `meta.availableFrom` identify incomplete historical
  coverage.

Preserve the API's educational disclaimer when presenting data in a student
application. If a project needs live, executable, guaranteed, or licensed
market data, use an appropriate commercial provider instead.
