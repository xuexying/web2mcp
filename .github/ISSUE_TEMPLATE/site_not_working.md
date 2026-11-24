---
name: Site not working
about: A site you are authorized to automate doesn't integrate well
labels: site-support
---

**Confirm authorization**
- [ ] I am authorized to automate this site/account and have checked its terms of service.

**What kind of site is it?** (SPA / server-rendered / GraphQL / RPC-over-one-URL / other)

**Which step fails?** (inspect / discover / trial-read / serve / write contract / login refresh)

**What does the failure look like?**

```text
# redacted command output — no cookies, tokens, HAR contents or real record data
```

**Anything notable about its API?** (non-JSON responses, dynamic signatures, CSRF in HTML,
anti-automation protection, …)

Note: sites behind CAPTCHAs / bot protection are intentionally unsupported — web2mcp will
not bypass them.
