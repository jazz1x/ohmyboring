---
id: eval-cors-preflight
title: Credentialed fetch blocked by a failing CORS preflight (OPTIONS)
kind: note
origin: personal
date: "2026-03-11"
tags:
  - cors
  - http
  - browser
  - frontend
tools:
  - nginx
  - fetch
concepts:
  - preflight request
  - access-control-allow-credentials
  - wildcard origin restriction
relates_to: []
summary: A credentialed cross-origin fetch failed because the server answered the OPTIONS preflight with Allow-Origin "*"; with credentials the origin must be echoed exactly and Allow-Credentials must be true.
---

# Credentialed fetch blocked by a failing CORS preflight

The browser refused a cross-origin `fetch(..., { credentials: "include" })` with a CORS error, and the network tab showed the `OPTIONS` preflight returning before the real request ever fired.

The server was replying with `Access-Control-Allow-Origin: *`. That wildcard is illegal once credentials (cookies / Authorization) are involved — the spec requires the server to echo the *exact* request origin and also send `Access-Control-Allow-Credentials: true`. The preflight must additionally allow the actual method and any custom headers via `Access-Control-Allow-Methods` and `Access-Control-Allow-Headers`.

Fix: reflect the validated `Origin` header (from an allowlist, not blind echo), set `Allow-Credentials: true`, and return `204` for `OPTIONS` with the matching `Allow-Methods`/`Allow-Headers`. The wildcard-with-credentials trap is the single most common preflight failure.
