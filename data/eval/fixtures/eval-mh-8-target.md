---
id: eval-mh-8-target
title: The de-AT catalog was left out of the packaging manifest glob
kind: note
origin: personal
date: "2026-01-12"
tags:
  - bundling
  - locales
tools:
  - rollup
concepts:
  - asset manifest
claims:
  - subject: web-client
    predicate: raw-key-cause
    value: locale catalog missing from the packaging manifest
summary: A glob in the asset manifest skipped the regional catalog, so the lookup fell through to identifiers.
---

Rollup config diff: the manifest glob `locales/??.json` matches exactly two characters
before the extension, so `de-AT.json` never entered `dist/`. Changed to `locales/*.json`,
and a post-bundle script exits 1 when a configured tag has no matching file in `dist/`.
