---
id: eval-mh-6-head
title: Websocket connections drop every few minutes on the office wifi
kind: note
origin: personal
date: "2026-03-15"
tags:
  - incident
  - realtime
tools:
  - socket-io
concepts:
  - connection stability
claims:
  - subject: realtime-gateway
    predicate: periodic-drop-cause
    value: nat idle timeout closes quiet flows after five minutes
summary: Websocket connections drop every few minutes on the office wifi; the cause is in an older note.
---

Websocket connections drop every few minutes on the office wifi, but stay up for hours on
mobile data. The drops happen even while the dashboard is open and nobody touches it. The
cause is written up in the older note that shares the claim below.
