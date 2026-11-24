# Web2MCP roadmap: from "framework prototype" to "paste a link, get MCP tools"

English | [简体中文](ROADMAP.zh-CN.md)

This file records the gaps between the current state and the goal, in priority order. It is
grounded in real integration trials from 2026-11: one real logged-in SPA was integrated
read-only (with browser-relayed sending), and a second real site was blocked by its
bot-protection layer — which is the expected, honest outcome there.

## Framing: a realistic goal, not a slogan

"Any website + any AI + zero-human full automation" is not reachable — logins, CAPTCHAs and
bot-protection layers (proof-of-work challenges, WAFs) exist precisely to stop automation.
The realistic goal is:

> **Most lightly-defended websites + one human login + an AI that can drive tools (or a
> configured model) → a usable read-only MCP server mostly automatically; writes enabled
> after review.**

The priorities below aim at that goal.

## Honest status (2026-11)

- The framework layer is real and tested (see CI): the HTTP → discovery → contract → MCP
  execution chain is solid.
- **No real model has ever driven a real website end to end**: autopilot
  (`serve-integrations`) has only been validated with a simulated model against local test
  sites; the default model config is empty.
- Browser-based capture needs `ego-browser` (optional, currently macOS and Windows); the
  HAR import path needs no browser tooling at all.
- The one verified real-site success was interactively AI-driven with several manual steps —
  not "automatic".

---

## P0 — make it usable by third parties (biggest blocker)

- **P0-1 Remove the hard `ego-browser` dependency; make the capture backend pluggable.**
  Add a publicly installable backend (Playwright / headless Chrome); ego-browser becomes one
  option. Done when: a stranger with only Python + Playwright completes a full capture.
- **P0-2 Promote HAR import to the first-class path.** HAR is the only browser-independent
  entrance; document "export HAR from DevTools → discover" as the primary flow.
  Done when: a user with no browser tooling gets an MCP server from a single HAR file.
- **P0-3 Proper packaging.** Publish to PyPI. Done when: `pip install web2mcp &&
  web2mcp --help` works out of the box.

## P1 — close the manual seams in the interactive flow

- **P1-1 Fix the browser space lifecycle bug.** An expired space_id reports "task space not
  found"; it should be recreated automatically.
- **P1-2 Remove the propose→synth ordering dependency.** Without a prior `explore-propose`,
  synth credentials are an unsupported placeholder.
- **P1-3 Synth is safe by default.** ✅ Done: `synth-profile` now defaults to
  `read_only_mode` and hides unverified writes.
- **P1-4 Auto-generate read trial cases.** Infer assertions from captured samples.
  Done when: capture → "read-only, trimmed, mountable" MCP takes 1–2 commands, not 6+.

## P2 — prove "real model, real site, autonomous" (core, currently zero evidence)

- **P2-1 Wire up real models.** Clean up model config and key handling (OpenAI-compatible /
  Anthropic).
- **P2-2 One real end-to-end site.** Pick a lightly-defended real site and let autopilot run
  from URL to published MCP; record success rate and break points, then iterate. This has
  never been done.
- **P2-3 Harden the planner loop.** Login question round-trips, budget exhaustion, recovery
  from consecutive failures — pressure-tested with a real model.
  Done when: at least one real site, real model, no human steps except login, produces a
  usable read-only MCP — with failures honestly recorded.

## P3 — a plug-and-play entrance for "any AI"

- **P3-1 Make web2mcp itself an MCP server.** Expose meta-tools `integrate(url, goal)` and
  `list_generated_tools()` so any MCP client can drive it (`serve-explorer` is the embryo).
- **P3-2 Smooth login relay.** Make "please log in in the browser" clean in remotely-driven
  scenarios.
  Done when: an ordinary MCP client can say "turn site X into tools" and get there (human
  supplies the login).

## P4 — breadth and robustness (ongoing)

- **Honest handling of anti-automation protection.** Detect CAPTCHAs / proof-of-work / WAF
  challenges, stop, and tell the user this site is not supported — recommend the official
  API instead. No bypassing.
- **SPA selector robustness.** Cope with click-to-mutate dynamic UIs.
- **Credential lifecycle.** Detect expired logins automatically and guide re-capture.

---

## Suggested starting point: P0-1 + P0-2 first

Capture-backend accessibility blocks everyone; an unwired model only blocks autopilot. Make
"any user can start" true first (Playwright backend + HAR as the main path), make the flow
smooth for humans (P0/P1), then attack the hardest and most uncertain piece — real-model
autonomy (P2).
