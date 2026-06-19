---
id: eval-tls-cert-renewal
title: Automating TLS certificate renewal after an expired cert took down the site
kind: note
origin: personal
date: "2026-02-13"
tags:
  - tls
  - certificates
  - ops
tools:
  - certbot
  - lets-encrypt
  - nginx
concepts:
  - certificate expiry
  - automated renewal
  - reload hooks
relates_to: []
summary: The site went down when a TLS cert expired unnoticed; certbot auto-renewal with a deploy hook and an expiry alert prevents a repeat.
---

The site returned `NET::ERR_CERT_DATE_INVALID` one morning — the TLS certificate had
expired and nobody noticed because renewal was a manual yearly task that got missed.

The durable fix had three parts. We moved to `certbot` with Let's Encrypt for
90-day certs and a `systemd` timer that renews them automatically well before expiry.
We added a deploy hook so nginx reloads the new cert without a manual restart. And we
set a monitoring alert that fires if any cert is within 14 days of expiry, as a
backstop in case automation silently breaks.

The lesson: certificate expiry is a predictable, scheduled outage waiting to happen.
Automate renewal and reload, and alert on remaining validity as a safety net.
