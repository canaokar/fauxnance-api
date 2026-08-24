"""Public, unauthenticated API reference (Scalar) served by the api Lambda.

The canonical contract lives in ``docs/openapi.yaml`` and is maintained by hand.
This module serves that file verbatim plus a small Scalar shell so the published
contract is browsable and the built-in API client can exercise the live
endpoints with an ``X-Api-Key``. Scalar parses YAML client-side, so no runtime
YAML dependency is required.
"""

from __future__ import annotations

from pathlib import Path

# Route paths are absolute-from-root: the HTTP API $default stage serves at the
# domain root (no stage segment), matching the servers URL in the spec.
DOCS_PATH = "/v1/docs"
OPENAPI_PATH = "/v1/openapi.yaml"

# Repo root is two levels above src/api/handler.py; the package preserves this
# layout, so docs/openapi.yaml resolves identically in Lambda and in tests.
_SPEC_PATH = Path(__file__).resolve().parents[2] / "docs" / "openapi.yaml"

# Pinned so a CDN republish can never change what the docs page loads, and
# integrity-checked so a compromised CDN cannot serve different bytes under the
# pinned URL. Regenerate the hash whenever the version moves:
#   curl -sL <bundle url> | openssl dgst -sha384 -binary | openssl base64 -A
_SCALAR_VERSION = "1.66.1"
_BUNDLE = (
    f"https://cdn.jsdelivr.net/npm/@scalar/api-reference@{_SCALAR_VERSION}"
    "/dist/browser/standalone.js"
)
_BUNDLE_SRI = "sha384-RkhHYpdjsrJH9sH8RmczPchxNiHEhmW300QwMB/8yg6feduTZu9FBN4W0DJnp50Z"

# The mount point holds a plain-HTML fallback until Scalar replaces it, so a
# blocked or unreachable CDN degrades to a pointer at the raw contract rather
# than a blank page.
_DOCS_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Fauxnance API — Reference</title>
  <link rel="icon" href="data:,">
  <style>
    body {{ margin: 0; }}
    #fallback {{
      margin: 4rem auto;
      max-width: 34rem;
      padding: 0 1.5rem;
      font: 1rem/1.6 system-ui, sans-serif;
    }}
  </style>
</head>
<body>
  <div id="api-reference">
    <div id="fallback">
      <h1>Fauxnance API</h1>
      <p>
        The interactive reference could not load. Read the contract directly at
        <a href="{OPENAPI_PATH}">{OPENAPI_PATH}</a>.
      </p>
    </div>
  </div>
  <script
    src="{_BUNDLE}"
    integrity="{_BUNDLE_SRI}"
    crossorigin="anonymous"
  ></script>
  <script>
    if (window.Scalar) {{
      Scalar.createApiReference("#api-reference", {{ url: "{OPENAPI_PATH}" }});
    }}
  </script>
</body>
</html>
"""

_spec_cache: str | None = None


def docs_html() -> str:
    """Return the Scalar reference page that points at :data:`OPENAPI_PATH`."""

    return _DOCS_HTML


def openapi_yaml() -> str:
    """Return the raw OpenAPI contract, reading it once and caching it."""

    global _spec_cache
    if _spec_cache is None:
        _spec_cache = _SPEC_PATH.read_text(encoding="utf-8")
    return _spec_cache
