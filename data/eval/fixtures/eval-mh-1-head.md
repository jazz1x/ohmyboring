---
id: eval-mh-1-head
title: Customers received the same invoice email twice after the scheduler restarted
kind: note
origin: personal
date: "2026-03-10"
tags:
  - incident
  - billing
tools:
  - cron
concepts:
  - duplicate delivery
claims:
  - subject: invoice-mailer
    predicate: duplicate-send-cause
    value: ledger rows carry no idempotency key
summary: Duplicate invoice emails went out after a scheduler restart; the cause was chased in a separate note.
---

After the scheduler was restarted during a deploy, customers received the same invoice
email twice. Support counted about forty affected accounts. The restart itself was clean,
so the duplicate delivery was not an obvious deployment mistake. The cause is written up
in the older note linked through the shared claim.
