"""The Playwright backend: run exploration rounds with a bundled Chromium.

Cross-platform and publicly installable (``pip install 'web2mcp[record]'`` plus
``playwright install chromium``). It reuses a persistent user-data dir inside the
session (``browser-profile/``), so a login done once carries across rounds without the
program ever handling the password.

Scope: the read-only mapping rounds that feed synth-profile — ``source_urls`` and
``survey`` — plus ``observe`` and bounded ``actions``, and a simple user-driven login
(``handoff``). Captured fetch/XHR exchanges are appended to ``capture.json`` in the same
``web2mcp-capture-v1`` shape the rest of the pipeline consumes. Passwords are never
persisted: any request carrying a password-like field is dropped before writing.

The ``cdp`` variant attaches to a Chrome the user already started with
``--remote-debugging-port`` instead of launching a new browser, reusing its live session.
"""

import os
import time
from pathlib import Path
from urllib.parse import urlsplit

from ..record import cookie_in_origin, has_password_input
from ..storage import load_json, save_private
from . import BrowserBackend, register

_READ_METHODS = ("GET", "HEAD")
# Links/elements that look like a state change are never auto-followed during a survey.
_SKIP = (
    "logout",
    "sign-out",
    "signout",
    "delete",
    "remove",
    "destroy",
    "cancel",
    "pay",
    "checkout",
    "退出",
    "登出",
    "删除",
    "注销",
    "支付",
    "付款",
    "取消",
)


def _settle_ms():
    try:
        return max(0, int(os.environ.get("WEB2MCP_SETTLE_MS", "")))
    except ValueError:
        return 8000


def _headful():
    return os.environ.get("WEB2MCP_HEADFUL", "").lower() in ("1", "true", "yes")


def _skip_url(url):
    low = url.lower()
    return any(token in low for token in _SKIP)


@register("playwright")
def _factory():
    return PlaywrightBackend()


@register("cdp")
def _cdp_factory():
    return PlaywrightBackend(attach=True)


class _Collector:
    """Buffers fetch/XHR exchanges seen on a page via Playwright response events."""

    def __init__(self, origins):
        self.origins = set(origins or [])
        self.exchanges = []

    def attach(self, page):
        page.on("response", self._on_response)

    def _on_response(self, response):
        try:
            request = response.request
            if request.resource_type not in ("fetch", "xhr"):
                return
            url = request.url
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https"):
                return
            if self.origins and f"{parts.scheme}://{parts.netloc}" not in self.origins:
                return
            headers = request.headers
            post = request.post_data
            if has_password_input(url, headers, post):
                return
            try:
                body = response.text()
            except Exception:
                body = None
            self.exchanges.append(
                {
                    "request": {"method": request.method, "url": url, "headers": headers, "postData": post},
                    "response": {
                        "status": response.status,
                        "headers": response.headers,
                        "mimeType": response.headers.get("content-type", ""),
                        "body": body,
                        "unavailable": body is None,
                    },
                    "label": "playwright round",
                }
            )
        except Exception:
            return  # a single response hiccup must not abort the round


def _cookie_records(cookies, origin):
    out = []
    for c in cookies:
        if cookie_in_origin(c, origin):
            out.append(
                {
                    "name": c.get("name"),
                    "value": c.get("value", ""),
                    "domain": c.get("domain", ""),
                    "path": c.get("path", "/"),
                    "secure": bool(c.get("secure")),
                    "httpOnly": bool(c.get("httpOnly")),
                    "expires": int(c.get("expires", -1) or -1),
                    "hostOnly": not str(c.get("domain", "")).startswith("."),
                }
            )
    return out


def _append_capture(root, collector, cookies, origins):
    """Merge this round's exchanges and a fresh cookie/session snapshot into capture.json."""
    path = Path(root) / "capture.json"
    previous = {"format": "web2mcp-capture-v1", "exchanges": [], "cookies": [], "sessions": {}}
    if path.exists():
        try:
            previous = load_json(path)
        except ValueError:
            pass
    captured_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    sessions = dict(previous.get("sessions") or {})
    all_cookie_records = []
    for origin in origins:
        records = _cookie_records(cookies, origin)
        if records:
            sessions[origin] = {"captured_at": captured_at, "cookies": records}
            all_cookie_records.extend(records)
    value = {
        "format": "web2mcp-capture-v1",
        "captured_at": captured_at,
        "exchanges": [*(previous.get("exchanges") or []), *collector.exchanges],
        "cookies": all_cookie_records or (previous.get("cookies") or []),
        "sessions": sessions,
    }
    save_private(path, value)
    return len(value["exchanges"])


class PlaywrightBackend(BrowserBackend):
    name = "playwright"

    def __init__(self, *, attach=False):
        self.attach = attach
        self.name = "cdp" if attach else "playwright"

    def available(self):
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            return False
        return True

    def unavailable_reason(self):
        if self.attach:
            return (
                "The cdp backend needs Playwright and a Chrome started with "
                "--remote-debugging-port=9222 (set WEB2MCP_CDP_URL if not the default). "
                "Install: pip install 'web2mcp[record]'."
            )
        return (
            "Playwright is not installed. Run: pip install 'web2mcp[record]' && playwright install chromium."
        )

    def run_round(self, root, run_id, *, timeout):
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        root = Path(root)
        payload = load_json(root / "rounds" / (run_id + ".input.json"))
        state = payload["state"]
        action = payload["action"]
        kind = action.get("kind", "observe")
        origins = state.get("origins", [])
        deadline = time.monotonic() + timeout
        output = root / "rounds" / (run_id + ".output.json")

        with sync_playwright() as p:
            try:
                context, owns = self._open_context(p, root, kind)
            except PlaywrightError as error:
                return {"ok": False, "stderr": f"Could not start Chromium: {error}"}
            try:
                page = context.pages[0] if context.pages else context.new_page()
                collector = _Collector(origins)
                collector.attach(page)
                result = self._dispatch(
                    kind, page, context, state, action, root, origins, collector, deadline
                )
                save_private(output, result)
                return {"ok": True}
            except PlaywrightError as error:
                return {"ok": False, "stderr": str(error)[:400]}
            finally:
                if owns:
                    try:
                        context.close()
                    except PlaywrightError:
                        pass

    def _open_context(self, p, root, kind):
        if self.attach:
            url = os.environ.get("WEB2MCP_CDP_URL", "http://localhost:9222")
            browser = p.chromium.connect_over_cdp(url)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            return ctx, False
        headful = _headful() or kind in ("handoff", "resume")
        ctx = p.chromium.launch_persistent_context(
            str(root / "browser-profile"),
            headless=not headful,
            viewport=None if headful else {"width": 1280, "height": 900},
        )
        return ctx, True

    def _navigate(self, page, url, deadline):
        from playwright.sync_api import Error as PlaywrightError

        try:
            page.goto(url, wait_until="commit", timeout=30000)
        except PlaywrightError:
            pass  # heavy SPAs may never fully settle; traffic still flows
        page.wait_for_timeout(min(_settle_ms(), max(0, int((deadline - time.monotonic()) * 1000))))

    def _dispatch(self, kind, page, context, state, action, root, origins, collector, deadline):
        if kind == "source_urls":
            return self._source_urls(page, state, deadline)
        if kind == "survey":
            return self._survey(page, state, action, root, origins, collector, deadline)
        if kind == "handoff":
            return self._handoff(page, state, deadline)
        if kind == "finish":
            return {"state": "finished", "space_id": state.get("space_id") or "playwright"}
        return self._observe(page, context, state, action, root, origins, collector, deadline)

    def _source_urls(self, page, state, deadline):
        self._navigate(page, state["url"], deadline)
        page.wait_for_timeout(2000)
        try:
            urls = page.evaluate(
                """() => {
                    const perf = performance.getEntriesByType('resource').map(r => r.name);
                    const tags = [...document.querySelectorAll('script[src]')].map(s => s.src);
                    return [...new Set([...perf, ...tags])].filter(u => /\\.m?js(\\?|$)/i.test(u));
                }"""
            )
        except Exception:
            urls = []
        return {"state": "source_urls", "js_urls": urls, "space_id": state.get("space_id") or "playwright"}

    def _handoff(self, page, state, deadline):
        # Open the window and let the user log in, then close it. Cookies persist to the
        # profile dir, so later rounds are authenticated without the program seeing secrets.
        self._navigate(page, state["url"], deadline)
        while time.monotonic() < deadline:
            try:
                if not page.context.pages:
                    break
            except Exception:
                break
            page.wait_for_timeout(1000)
        return {"state": "login_complete", "space_id": state.get("space_id") or "playwright"}

    def _in_scope(self, url, origins):
        try:
            parts = urlsplit(url)
            return f"{parts.scheme}://{parts.netloc}" in origins
        except ValueError:
            return False

    def _survey(self, page, state, action, root, origins, collector, deadline):
        max_pages = min(max(1, action.get("max_pages", 20)), 60)
        seen, visited, errors = set(), [], []
        queue = [state["url"]]
        while queue and len(visited) < max_pages and time.monotonic() < deadline:
            url = queue.pop(0)
            key = url.split("#")[0]
            if key in seen:
                continue
            seen.add(key)
            if not self._in_scope(url, origins) or _skip_url(url):
                continue
            try:
                self._navigate(page, url, deadline)
            except Exception as error:
                errors.append({"url": url, "error": str(error)[:80]})
                continue
            visited.append(url)
            try:
                links = page.evaluate("() => [...document.querySelectorAll('a[href]')].map(a => a.href)")
            except Exception:
                links = []
            for link in links:
                k = link.split("#")[0]
                if k and k not in seen and self._in_scope(link, origins) and not _skip_url(link):
                    queue.append(link)
        total = _append_capture(root, collector, self._cookies(page), origins)
        return {
            "state": "surveyed",
            "pages": len(visited),
            "visited": visited,
            "errors": errors,
            "total_exchanges": total,
            "space_id": state.get("space_id") or "playwright",
        }

    def _observe(self, page, context, state, action, root, origins, collector, deadline):
        # The collector is already listening (attached before navigation). On the first
        # round there is no page yet, so navigate to the start URL; later rounds act on
        # the page the persistent context restored.
        first = state.get("space_id") is None
        if first:
            self._navigate(page, state["url"], deadline)
        for step in action.get("steps", []):
            kind = step["type"]
            if kind == "goto":
                self._navigate(page, step["url"], deadline)
            elif kind == "click":
                page.click(step["selector"])
            elif kind == "fill":
                page.fill(step["selector"], step.get("value", ""))
            elif kind == "select":
                page.select_option(step["selector"], step.get("value"))
            elif kind == "press":
                page.press(step["selector"], step.get("value", ""))
            if step.get("wait_selector"):
                page.wait_for_selector(step["wait_selector"], state="visible", timeout=10000)
            if not self._in_scope(page.url, origins):
                raise RuntimeError("Navigation left reviewed scope; inspect before continuing")
        if action.get("wait_selector"):
            page.wait_for_selector(action["wait_selector"], state="visible", timeout=10000)
        page.wait_for_timeout(min(1500, max(0, int((deadline - time.monotonic()) * 1000))))
        total = _append_capture(root, collector, self._cookies(page), origins)
        try:
            fields = page.evaluate(
                """() => [...document.querySelectorAll('input,select,textarea')]
                    .filter(e => e.getClientRects().length && e.type !== 'hidden').slice(0, 100)
                    .map(e => ({tag: e.tagName.toLowerCase(), type: e.type, name: e.name, id: e.id,
                        required: e.required, disabled: e.disabled,
                        label: [...(e.labels||[])].map(l => l.textContent.trim()).join(' ')}))"""
            )
        except Exception:
            fields = []
        state["space_id"] = state.get("space_id") or "playwright"
        return {
            "goal": state.get("goal"),
            "space_id": state["space_id"],
            "page": "p1",
            "url": page.url,
            "fields": fields,
            "capture": {"captured": len(collector.exchanges), "total": total},
            "remaining_actions": state.get("budget", 0) - state.get("actions_used", 0),
            "content_trust": "untrusted_product_data",
        }

    def _cookies(self, page):
        try:
            return page.context.cookies()
        except Exception:
            return []
