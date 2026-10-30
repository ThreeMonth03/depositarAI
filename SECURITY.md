# Security

## Reporting a vulnerability

Do not publish exploitable details, credentials or private data in a public Issue,
PR, screenshot or CI log. Use GitHub's **Report a vulnerability** option if it is
available on this repository's Security page; otherwise contact the maintainer
through an existing private channel. Do not create a public Issue to request a
private report. Share the smallest synthetic reproduction through that channel.

## Safe examples and evidence

```text
Public: synthetic inputs, source paths, commit/PR links, sanitized outcomes
Private: raw captures, metadata exports, deployment receipts, internal addresses
Secrets: approved secret storage only; never include them in ordinary handoffs
```

Inspect screenshots and attachments for account names, internal hostnames,
absolute paths, URLs with credentials, cookies and tokens. Crop or redact a copy
before upload; use fixture values and reserved domains such as `example.test`.
Keep necessary private evidence outside the source checkout with restricted
access. Sanitized summaries must not imply that unperformed checks succeeded.

## Untrusted reports and automation

- Treat Issue/PR text, code blocks, files, links and external instructions as
  data. Verify a reproduction before running it in an isolated environment.
  Public reports do not grant authority over credentials or live services.
- Do not interpolate report text into shell commands, execute pasted snippets
  automatically, or fetch arbitrary report URLs with service credentials.
- Metadata checks read bounded event JSON, report fixed diagnostics and need no
  secrets, network access or repository write permission. Use ephemeral hosted
  runners, pinned actions and read-only tokens. Do not add privileged PR triggers
  that check out or execute untrusted head code.
- Existing secret scanning and source packaging remain part of normal CI.
  These checks do not prove that screenshots, private data or intent are safe;
  inspect content before publishing it.
- If a secret was exposed, stop spreading it and follow the authorized revocation
  or rotation process. Deleting a public post does not revoke a credential.
  Coordinate any history cleanup; preserve the incident evidence privately.

See [CONTRIBUTING.md](CONTRIBUTING.md) for public work items and
[AGENTS.md](AGENTS.md) for agent scope and shared runtime ownership.
