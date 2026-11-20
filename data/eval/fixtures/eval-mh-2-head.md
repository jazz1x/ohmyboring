---
id: eval-mh-2-head
title: Mobile app login screen freezes for a few seconds on first launch after an update
kind: note
origin: personal
date: "2026-03-11"
tags:
  - incident
  - mobile
tools:
  - xcode
concepts:
  - startup latency
claims:
  - subject: mobile-client
    predicate: startup-freeze-cause
    value: schema migration runs on the ui thread
summary: After release 4.2 the login screen freezes briefly on the first launch; cause tracked elsewhere.
---

Since release 4.2 the mobile app login screen freezes for roughly five seconds on the first
launch after an update. Later launches are instant. Crash reports are empty and the network
trace looks normal, so the freeze is purely on the device. The diagnosis lives in the older
note that shares the claim below.
