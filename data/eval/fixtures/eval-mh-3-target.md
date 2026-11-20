---
id: eval-mh-3-target
title: Denormalized cost field refreshed only by a nightly batch
kind: note
origin: personal
date: "2026-01-07"
tags:
  - denormalization
  - indexing
tools:
  - kafka
concepts:
  - cache invalidation
claims:
  - subject: catalog-pricing
    predicate: stale-price-cause
    value: nightly batch refresh instead of change events
summary: The copied cost field in the lookup index was rebuilt once a night; change events fix the lag.
---

Replace the 03:00 rebuild job with a change-data-capture consumer. The catalog table's
write-ahead log is published to a Kafka topic keyed by item id, so all updates for one item
land on one partition and apply in order. A consumer group of four rewrites the denormalized
column in the lookup index with an idempotent upsert. The old batch remains as a weekly
reconciliation that only reports drift.
