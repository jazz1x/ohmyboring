---
id: eval-mh-6-target
title: The router's NAT table forgot quiet flows after five minutes
kind: note
origin: personal
date: "2026-01-10"
tags:
  - networking
  - keepalive
tools:
  - tcpdump
concepts:
  - heartbeat
claims:
  - subject: realtime-gateway
    predicate: periodic-drop-cause
    value: nat idle timeout closes quiet flows after five minutes
summary: Address translation at the router expired silent flows; periodic heartbeat frames keep them alive.
---

tcpdump trace: last segment at t=0, silence until t=300 s, then a RST. The router's
conntrack entry for an established flow expires after 300 s of no traffic
(`nf_conntrack_tcp_timeout_established`). Setting `TCP_KEEPIDLE=30` and an application ping
every 25 s keeps the entry refreshed.
