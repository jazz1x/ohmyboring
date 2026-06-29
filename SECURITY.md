# Security Policy

## Supported versions

ohmyboring is at `0.1.0`. Security fixes target the latest release and `main`.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately via **GitHub Security Advisories** — on the repository, go to
the **Security** tab → **Report a vulnerability** (the "Private vulnerability
reporting" form). This opens a private channel with the maintainer. If that is
unavailable, contact the maintainer directly through their GitHub profile.

When reporting, please include:

- a description of the issue and its impact,
- steps to reproduce (or a proof of concept),
- affected version or commit, and your environment (OS, Docker / Native mode).

We will acknowledge the report, investigate, and coordinate a fix and
disclosure with you.

## Security model

ohmyboring is **self-hosted and local-first** — there is no cloud component.
A couple of properties are worth knowing when assessing risk:

- **It runs a localhost HTTP server.** The `drudge` engine serves an HTTP API
  (default port `7700`). In Docker it is published to `127.0.0.1:7700`
  (loopback only). In Native mode it binds `0.0.0.0:7700` by default — set
  `BORING_HTTP_ADDR=127.0.0.1:7700` to keep it loopback-only. The API is
  unauthenticated, so do **not** expose the port to an untrusted network.
- **It scrubs secrets and PII at the ingest boundary.** Before any text is written to
  the vault, every rendered field is passed through a secret redactor
  (`drudge/src/redact.rs`) and a shape-based PII gate (`drudge/src/pii.rs`).
  The secret redactor replaces matches with `‹REDACTED›`; the PII gate can block,
  redact, or flag sensitive shapes based on `vault/rules/pii.yaml` plus an optional
  gitignored `vault/rules/pii.local.yaml` overlay. CI layers defence in depth on top:
  `gitleaks` (secret scan) and `trivy` (filesystem / secret / misconfig scan).
  Redaction and PII matching are best-effort pattern matching, not a guarantee —
  review notes before sharing your vault, and treat the vault as potentially
  sensitive personal data.

Because the vault and the vector DB live entirely on your machine, securing the
host (disk encryption, file permissions, not exposing ports) is part of the
threat model.
