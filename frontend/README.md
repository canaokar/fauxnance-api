# Fauxnance instructor console — frontend

Plain HTML/CSS/vanilla JS. No build step, no dependencies.

## Deploy to Cloudflare Pages

- Build command: none
- Output directory: `frontend`

Connect the repo (or upload the `frontend/` directory directly) in the
Cloudflare Pages dashboard using those two settings. There is nothing to
compile — Pages serves the files as-is.

## Point it at a different API

Edit `frontend/config.js`. It is the only file you need to change:

```js
window.FAUXNANCE_API = "https://your-api-id.execute-api.eu-west-2.amazonaws.com";
```

## Session storage

The signed-in session token is held in `sessionStorage` under the key
`fauxnance.session`. It is cleared automatically when the browser tab is
closed, and is also cleared (with a redirect to the login screen) whenever
the API responds with `401`.
