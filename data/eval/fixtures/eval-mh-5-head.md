---
id: eval-mh-5-head
title: Unit tests pass locally but the pipeline run randomly turns red on timestamps
kind: note
origin: personal
date: "2026-03-14"
tags:
  - incident
  - ci
tools:
  - github-actions
concepts:
  - flaky builds
claims:
  - subject: ci-pipeline
    predicate: timestamp-flake-cause
    value: runners use utc while developer machines use local time
summary: Timestamp assertions pass locally and fail on the pipeline; the cause is in an older note.
---

Unit tests that compare timestamps pass locally but the pipeline run randomly turns red.
Re-running the failed job sometimes makes it green, and the failures cluster in the late
evening. The reason is in the older note that shares the claim below.
