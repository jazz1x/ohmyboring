---
id: eval-docker-layer-cache
title: Speeding up Docker builds by ordering layers for cache reuse
kind: note
origin: personal
date: "2026-02-05"
tags:
  - docker
  - build
  - caching
tools:
  - docker
  - buildkit
concepts:
  - layer caching
  - dependency manifest
  - cache invalidation
relates_to: []
summary: A Dockerfile reinstalled all dependencies on every code change; copying the manifest before the source restored layer-cache reuse and cut build time.
---

Every CI build reinstalled the full dependency tree even when only application code
changed, so builds took eight minutes. The Dockerfile did `COPY . .` before the
dependency install, which meant any source edit invalidated the install layer.

The fix was to copy only the dependency manifest first, run the install, and then
copy the rest of the source:

```
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
```

Now the expensive install layer is reused from the BuildKit cache whenever the
manifest is unchanged, and only the cheap source-copy layer rebuilds. Build time
dropped from eight minutes to under one. The principle: order Dockerfile steps from
least- to most-frequently-changing so the cache survives ordinary edits.
