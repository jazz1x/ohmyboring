---
id: eval-mh-1-target
title: Outbox ledger needs a unique dedupe token per message
kind: note
origin: personal
date: "2026-01-05"
tags:
  - outbox
  - reliability
tools:
  - postgres
concepts:
  - idempotency
claims:
  - subject: invoice-mailer
    predicate: duplicate-send-cause
    value: ledger rows carry no idempotency key
summary: The outbox ledger had no unique dedupe token, so a replayed batch was dispatched again.
---

Schema change for the outbox ledger: add a `dedupe_token text NOT NULL` column and a unique
index on it. The token is a SHA-256 over message kind, recipient id and billing period, so
one logical message always maps to one token. Inserts use `ON CONFLICT DO NOTHING`
and the worker only dispatches rows it actually inserted. Existing rows were backfilled in
batches of 5,000 with that derivation before the index was created concurrently.
