---
id: eval-mh-8-head
title: Translation strings show raw keys in the German build only
kind: note
origin: personal
date: "2026-03-17"
tags:
  - incident
  - i18n
tools:
  - webpack
concepts:
  - localization
claims:
  - subject: web-client
    predicate: raw-key-cause
    value: locale catalog missing from the packaging manifest
summary: Raw translation keys appear in the German build only; the cause is in an older note.
---

Translation strings show raw keys in the German build only. The English and French builds
look correct, and the key names are present in the source repository. The cause is written
up in the older note that shares the claim below.
