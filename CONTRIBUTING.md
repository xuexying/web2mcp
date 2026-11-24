# Contributing

Thanks for your interest in Web2MCP!

## Running the tests

```sh
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q
python examples/demo.py          # end-to-end MCP check, fully offline
ruff check . && ruff format --check .
```

All tests run against synthetic loopback fixtures — no real accounts, sites or credentials
are involved, and contributions must keep it that way.

## Pull requests

- Keep changes focused; one topic per PR.
- Add or update tests for behavior changes. `python -m pytest -q` and
  `python examples/demo.py` must pass.
- Run `ruff format` before committing.
- Never include captures, HAR files, credentials, cookies, tokens, internal hostnames or
  real business data — in code, tests, fixtures or commit messages. The `.gitignore`
  covers the usual private files; please don't fight it.

## Submitting an integration pack

Integration packs (`*.pack.json` + `policy.json`) describe how to call a product and contain
no credentials, so they are shareable. When contributing one:

1. Target software the reviewer can reproduce — self-hostable open-source web apps are ideal.
2. Include the policy, the pack, and a short README describing which app/version it was
   validated against and which operations were trialed.
3. Make sure it contains no credentials, no real record contents, and no internal URLs.
4. Read/write classification must be explicit; side-effect endpoints are never `read_only`.

## Reporting issues

Use the issue templates: bug report, "site not working", or feature request. For security
problems, see [SECURITY.md](SECURITY.md) — please don't open a public issue.
