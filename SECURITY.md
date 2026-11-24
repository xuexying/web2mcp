# Security policy

## Scope and intended use

Web2MCP automates HTTP calls that a logged-in user recorded in their own browser. **It is
only intended for sites and accounts you are authorized to automate.** It deliberately does
not bypass CAPTCHAs, proof-of-work challenges or other anti-automation protection: when such
protection is detected the supported behavior is to stop and recommend the official API.

## Reporting a vulnerability

Please report security issues privately via GitHub's **Report a vulnerability** (Security →
Advisories → Report a vulnerability) on this repository, rather than opening a public issue.

Include what you can: affected version/commit, reproduction steps, and impact. You should
get an initial response within a week.

## What counts as a vulnerability here

Especially interesting reports:

- Credential leakage: captured cookies/tokens ending up in generated code, OpenAPI output,
  audit logs, diagnostics or error messages (they belong only in the private
  `credentials.json`).
- Write-safety bypasses: executing a write without preview + confirmation, defeating the
  duplicate-submission lock, or smuggling a write through a `read_only` / `read_only_mode`
  profile.
- Scope escapes: requests leaving the reviewed `allowed_origins`, or exploration escaping
  its session directory.
- Injection from untrusted data: captured pages/responses influencing the executor beyond
  their role as data (prompt-injection hardening of the planner loop included).
