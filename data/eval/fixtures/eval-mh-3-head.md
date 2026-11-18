---
id: eval-mh-3-head
title: Search results page returns stale prices for some products
kind: note
origin: personal
date: "2026-03-12"
tags:
  - incident
  - catalog
tools:
  - elasticsearch
concepts:
  - data freshness
claims:
  - subject: catalog-pricing
    predicate: stale-price-cause
    value: nightly batch refresh instead of change events
summary: Some products show an outdated price on the search results page; the cause is documented in an older note.
---

Shoppers reported that the search results page returns stale prices for some products while
the product detail view shows the right amount. The mismatch lasts until the next morning
and then disappears on its own. The explanation is in the older note sharing the claim below.
