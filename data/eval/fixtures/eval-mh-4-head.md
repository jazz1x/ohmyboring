---
id: eval-mh-4-head
title: File uploads larger than a gigabyte fail behind the load balancer
kind: note
origin: personal
date: "2026-03-13"
tags:
  - incident
  - storage
tools:
  - nginx
concepts:
  - large transfers
claims:
  - subject: upload-gateway
    predicate: big-transfer-failure-cause
    value: request body buffered to disk with a sixty second idle timeout
summary: Large file uploads fail only behind the load balancer; the cause is in an older note.
---

File uploads larger than a gigabyte fail behind the load balancer with a gateway error,
while the same upload straight to the application server works. Smaller files are fine.
The cause is recorded in the older note that shares the claim below.
