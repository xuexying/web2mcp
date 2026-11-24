"""Pluggable browser backends for the exploration flow.

A backend runs one exploration round: it reads ``rounds/<run_id>.input.json``
(written by ``explore``), drives a browser, and writes ``rounds/<run_id>.output.json``
plus an appended ``capture.json``. ``explore`` stays backend-agnostic — it builds the
input, asks a backend to run the round, and reads the output.

Backends:
- ``ego``         — the original ego-browser bridge (``ego_explorer.mjs``), macOS/Windows.
- ``playwright``  — a bundled Chromium via Playwright (``pip install 'web2mcp[record]'``),
                    cross-platform; covers the read-only mapping rounds and bounded UI actions.
- ``cdp``         — attach to a Chrome the user already launched with a remote-debugging
                    port, reusing its live login state (Playwright under the hood).

Selection: an explicit name (``--backend`` / ``WEB2MCP_BACKEND``) wins; otherwise the
first available backend is used (ego, then playwright). When none is available the error
tells the user how to proceed, rather than only naming a missing executable.
"""

import os

from ..client import CallError

_REGISTRY = {}


def register(name):
    def decorator(factory):
        _REGISTRY[name] = factory
        return factory

    return decorator


def _load_registry():
    # Import lazily so an absent optional dependency (Playwright) never breaks import.
    from . import ego as _ego  # noqa: F401
    from . import playwright_backend as _pw  # noqa: F401


_AUTO_ORDER = ("ego", "playwright", "cdp")

NO_BACKEND_HINT = (
    "No browser backend is available for exploration. Either install the bundled recorder "
    "(pip install 'web2mcp[record]' && playwright install chromium), install ego-browser "
    "(https://github.com/citrolabs/ego-lite), or skip exploration and import a HAR instead "
    "(web2mcp inspect site.har)."
)


def available_backends():
    _load_registry()
    return {name: factory for name, factory in _REGISTRY.items() if factory().available()}


def get_backend(name=None):
    """Return a ready backend. ``name`` (or ``WEB2MCP_BACKEND``) forces one; otherwise the
    first available of ego → playwright → cdp is used. Raises a guiding CallError if the
    requested backend is unusable or nothing is available."""
    _load_registry()
    requested = name or os.environ.get("WEB2MCP_BACKEND") or None
    if requested:
        factory = _REGISTRY.get(requested)
        if factory is None:
            raise CallError(
                f"Unknown browser backend {requested!r}; choose from {sorted(_REGISTRY)}",
                reason="browser_unavailable",
            )
        backend = factory()
        if not backend.available():
            raise CallError(backend.unavailable_reason() or NO_BACKEND_HINT, reason="browser_unavailable")
        return backend
    for candidate in _AUTO_ORDER:
        factory = _REGISTRY.get(candidate)
        if factory and factory().available():
            return factory()
    raise CallError(NO_BACKEND_HINT, reason="browser_unavailable")


class BrowserBackend:
    """Runs one exploration round against ``rounds/<run_id>.input.json``.

    ``run_round`` must leave ``rounds/<run_id>.output.json`` on success (and append any
    captured exchanges to ``capture.json``); ``explore`` reads that file. A bounded
    ``timeout`` is supplied; exceeding it should raise ``TimeoutError``."""

    name = "base"

    def available(self):
        raise NotImplementedError

    def unavailable_reason(self):
        return None

    def run_round(self, root, run_id, *, timeout):
        raise NotImplementedError
