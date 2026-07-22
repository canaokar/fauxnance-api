"""Public, unauthenticated API reference (Swagger UI) served by the api Lambda.

The canonical contract lives in ``docs/openapi.yaml`` and is maintained by hand.
This module serves that file verbatim plus a small Swagger UI shell so the
published contract is browsable and the "Try it out" panel can exercise the
live endpoints with an ``X-Api-Key``. Swagger UI parses YAML client-side, so no
runtime YAML dependency is required.
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

# Pinned so a CDN republish can never change what the docs page loads.
_SWAGGER_UI_VERSION = "5.17.14"
_CDN = f"https://unpkg.com/swagger-ui-dist@{_SWAGGER_UI_VERSION}"

_DOCS_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Fauxnance API — Reference</title>
  <link rel="icon" href="data:,">
  <link rel="stylesheet" href="{_CDN}/swagger-ui.css">
  <style>body {{ margin: 0; }} .swagger-ui .topbar {{ display: none; }}</style>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="{_CDN}/swagger-ui-bundle.js" crossorigin></script>
  <script src="{_CDN}/swagger-ui-standalone-preset.js" crossorigin></script>
  <script>
    window.ui = SwaggerUIBundle({{
      url: "{OPENAPI_PATH}",
      dom_id: "#swagger-ui",
      deepLinking: true,
      presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset],
      layout: "StandaloneLayout",
    }});
  </script>
</body>
</html>
"""

_spec_cache: str | None = None


def docs_html() -> str:
    """Return the Swagger UI page that points at :data:`OPENAPI_PATH`."""

    return _DOCS_HTML


def openapi_yaml() -> str:
    """Return the raw OpenAPI contract, reading it once and caching it."""

    global _spec_cache
    if _spec_cache is None:
        _spec_cache = _SPEC_PATH.read_text(encoding="utf-8")
    return _spec_cache
