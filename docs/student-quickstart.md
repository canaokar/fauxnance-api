# Student quickstart

Fauxnance provides delayed educational market data for capstone projects. It is
not an investment service. The current base URL is the default API Gateway URL:

```text
https://y4t9nq2bqf.execute-api.eu-west-2.amazonaws.com/v1
```

No custom domain is configured. Browse the interactive Swagger UI at
[the `/v1/docs` route](https://y4t9nq2bqf.execute-api.eu-west-2.amazonaws.com/v1/docs)
or download the contract from `/v1/openapi.yaml`.

Your instructor will give you a key beginning with `fnx_`. Treat it like a
password: keep it out of source control, screenshots, notebooks, shared
workbooks, and chat messages.

## curl

Keep configuration in environment variables and send the key in the
`X-Api-Key` header:

```sh
export FNX_BASE_URL="https://y4t9nq2bqf.execute-api.eu-west-2.amazonaws.com/v1"
export FNX_API_KEY="paste-your-issued-key-here"

curl --fail-with-body --silent --show-error \
  -H "X-Api-Key: $FNX_API_KEY" \
  "$FNX_BASE_URL/quotes?symbols=AAPL,INFY.NS,FX:EURUSD,X:BTC-USD"
```

Historical daily candles use inclusive ISO dates:

```sh
curl --fail-with-body --silent --show-error \
  -H "X-Api-Key: $FNX_API_KEY" \
  "$FNX_BASE_URL/candles/AAPL?from=2025-01-01&to=2025-12-31&interval=1d"
```

## Python

This example uses only Python's standard library:

```python
import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen

base_url = os.environ.get(
    "FNX_BASE_URL",
    "https://y4t9nq2bqf.execute-api.eu-west-2.amazonaws.com/v1",
)
query = urlencode({"symbols": "AAPL,INFY.NS,FX:EURUSD"})
request = Request(
    f"{base_url}/quotes?{query}",
    headers={"X-Api-Key": os.environ["FNX_API_KEY"]},
)

with urlopen(request, timeout=15) as response:
    payload = json.load(response)

for item in payload["data"]["quotes"]:
    if "quote" in item:
        print(item["symbol"], item["quote"]["price"], item["source"])
    else:
        print(item["symbol"], item["error"]["code"])
```

Catch `urllib.error.HTTPError` and branch on its `code`; see the
[error-handling guide](error-handling.md) before adding retries.

## Browser JavaScript

API Gateway allows browser requests from local student projects:

```js
const baseUrl =
  "https://y4t9nq2bqf.execute-api.eu-west-2.amazonaws.com/v1";
const apiKey = sessionStorage.getItem("FNX_API_KEY");

const response = await fetch(`${baseUrl}/quotes/AAPL`, {
  headers: { "X-Api-Key": apiKey },
});
const payload = await response.json();

if (!response.ok) {
  throw new Error(payload.error?.code ?? `HTTP_${response.status}`);
}
console.log(payload.data, payload.meta);
```

Any key used directly in a browser is visible to that browser's user. This is
acceptable for a local teaching prototype, but never hard-code the key into a
JavaScript bundle or deploy it as a public website secret. Use a small backend
proxy if the project becomes public.

## Excel Power Query

In Power Query, create text parameters named `FauxnanceBaseUrl` and
`FauxnanceApiKey`. Use the base URL above, then create a blank query and open
the Advanced Editor:

```powerquery
let
    Payload = Json.Document(
        Web.Contents(
            FauxnanceBaseUrl,
            [
                RelativePath = "quotes",
                Query = [symbols = "AAPL,INFY.NS,FX:EURUSD"],
                Headers = [#"X-Api-Key" = FauxnanceApiKey],
                Timeout = #duration(0, 0, 0, 15)
            ]
        )
    ),
    Quotes = Payload[data][quotes],
    Table = Table.FromRecords(Quotes)
in
    Table
```

Power Query stores parameter values in the workbook. Remove the key before
sharing the file, or distribute a key-free template and have each student set
their own parameter locally. Do not put the key in a visible worksheet cell.

## Reading responses

Successful responses contain `data` and `meta`. Inspect `meta.source`,
`meta.stale`, and candle-level `synthetic` flags instead of assuming every value
is a fresh exchange observation. Batch quote requests return HTTP `200` even
when one symbol has an item-level `error`.

Check your remaining allowance with `GET /v1/usage`; that request also consumes
one quota unit. Check `GET /v1/health` without a key when deciding whether a
problem is local or service-wide.
