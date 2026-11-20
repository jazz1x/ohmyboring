---
id: eval-mh-4-target
title: Gateway buffered the payload to disk and hit its idle timeout
kind: note
origin: personal
date: "2026-01-08"
tags:
  - proxy
  - timeouts
tools:
  - envoy
concepts:
  - request buffering
claims:
  - subject: upload-gateway
    predicate: big-transfer-failure-cause
    value: request body buffered to disk with a sixty second idle timeout
summary: The proxy spooled the whole payload to disk before forwarding and its idle timer expired.
---

Config diff for the edge proxy: `proxy_request_buffering off`, `client_max_body_size 0`,
`proxy_send_timeout 600s`. With buffering on, nginx spools each body into
`/var/cache/nginx/client_temp` and sends nothing upstream until the spool completes, so
the 60 s idle timer on the gateway expired first and the response was a 504.
