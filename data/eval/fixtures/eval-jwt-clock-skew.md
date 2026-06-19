---
id: eval-jwt-clock-skew
title: JWT validation failing intermittently because of server clock skew
kind: note
origin: personal
date: "2026-02-03"
tags:
  - jwt
  - auth
  - clock-skew
tools:
  - jsonwebtoken
  - ntp
  - chrony
concepts:
  - token expiry
  - leeway
  - clock synchronization
relates_to: []
summary: Intermittent "token used before issued" JWT errors came from drifting server clocks; a small validation leeway plus NTP fixed it.
---

Users were randomly getting logged out and the auth logs showed
`token used before issued (iat)` and occasional `token expired` errors even on
fresh tokens. The tokens were valid — the problem was that the issuing host and the
validating host had clocks that drifted a few seconds apart.

Two fixes. We enabled a 30-second validation **leeway** in the `jsonwebtoken`
verifier so `nbf`/`iat`/`exp` checks tolerate small skew. More importantly we made
sure every host runs `chrony`/NTP so the clocks stay synchronized in the first place.

The takeaway: any time-based token check is only as reliable as the clocks on both
ends. Add a bounded leeway for jitter, but fix the root cause with clock sync.
