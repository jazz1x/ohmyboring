---
id: eval-mh-5-target
title: Runners use UTC while developer machines use local time
kind: note
origin: personal
date: "2026-01-09"
tags:
  - timezone
  - determinism
tools:
  - jest
concepts:
  - clock injection
claims:
  - subject: ci-pipeline
    predicate: timestamp-flake-cause
    value: runners use utc while developer machines use local time
summary: Date assertions depended on the host zone; injecting a clock and pinning the zone removed the flake.
---

Hosted runners keep `TZ=UTC`; laptops in Seoul sit at UTC+9, so between 15:00 and 24:00 UTC
the calendar day differs by one. Production code now takes a `Clock` interface instead of
calling `Date.now()`, the specs install a frozen clock at a fixed instant, and the workflow
file exports `TZ=UTC` for every job so every host computes the same day boundary.
