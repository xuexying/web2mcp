"""`web2mcp record <url>`: cross-platform one-command capture via Playwright.

Opens a regular Chromium window; the user logs in and clicks through the app
themselves, then closes the window to finish. The recorded traffic becomes a
standard `capture.json` for the existing discover pipeline. Passwords and
one-time codes typed by the user are never persisted: any request carrying a
password-like field is dropped before anything is written, and the raw HAR is
deleted after conversion. Playwright is an optional dependency
(``pip install 'web2mcp[record]'``); the HAR import path never needs it.
"""

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .client import CallError
from .discover import _looks_json, normalize_capture, summarize_capture
from .storage import load_json, save_private

_PASSWORD_KEY = re.compile(r"password|passwd|passphrase|^(pwd|passcode|otp)$", re.I)
_PASSWORD_FORM = re.compile(r"name=[\"'](?:password|passwd|pwd|passcode|otp)[\"']", re.I)

PLAYWRIGHT_HINT = (
    "Playwright is not available. Install the recorder with: "
    "pip install 'web2mcp[record]' && playwright install chromium — "
    "or export a HAR from any browser instead (then: web2mcp inspect site.har)."
)


def _contains_password(value):
    if isinstance(value, list):
        return any(_contains_password(item) for item in value)
    if isinstance(value, dict):
        return any(_PASSWORD_KEY.search(key) or _contains_password(item) for key, item in value.items())
    if isinstance(value, str) and value.lstrip()[:1] in "[{":
        try:
            return _contains_password(json.loads(value))
        except ValueError:
            return False
    return False


def has_password_input(url, headers, post_text):
    """True when a request carries password/OTP material. Never persist such requests:
    cookies and session tokens are the explicitly authorized relay data, typed secrets
    are not. ``headers`` is a plain name->value mapping; ``post_text`` the raw body."""
    for key, value in (headers or {}).items():
        if _PASSWORD_KEY.search(key):
            return True
        if key.lower() in ("authorization", "proxy-authorization") and re.match(
            r"^Basic\s", str(value), re.I
        ):
            return True
    try:
        query = parse_qsl(urlsplit(url).query, keep_blank_values=True)
    except ValueError:
        query = []
    if any(_PASSWORD_KEY.search(key) or _contains_password(value) for key, value in query):
        return True
    if not post_text:
        return False
    try:
        if _contains_password(json.loads(post_text)):
            return True
    except ValueError:
        pass
    if any(
        _PASSWORD_KEY.search(key) or _contains_password(value)
        for key, value in parse_qsl(post_text, keep_blank_values=True)
    ):
        return True
    return bool(_PASSWORD_FORM.search(post_text))


def _entry_has_password(entry):
    request = entry.get("request", {})
    headers = {h.get("name", ""): h.get("value", "") for h in request.get("headers", []) or []}
    post = (request.get("postData") or {}).get("text")
    return has_password_input(request.get("url", ""), headers, post)


def _api_like(entry):
    """Keep API traffic, drop page assets: JSON responses, and any non-GET/HEAD or
    body-carrying request (its outcome may matter even when the response is empty)."""
    request = entry.get("request", {})
    method = (request.get("method") or "GET").upper()
    if method not in ("GET", "HEAD") or (request.get("postData") or {}).get("text"):
        return True
    response = entry.get("response", {}) or {}
    content = response.get("content", {}) or {}
    return _looks_json({"mimeType": content.get("mimeType", ""), "body": content.get("text")})


def cookie_in_origin(cookie, origin):
    host = (urlsplit(origin).hostname or "").lower()
    domain = str(cookie.get("domain") or "").lower()
    bare = domain.lstrip(".")
    return bool(bare) and (host == bare or (domain.startswith(".") and host.endswith("." + bare)))


def _cookie_record(cookie):
    return {
        "name": cookie.get("name"),
        "value": cookie.get("value", ""),
        "domain": cookie.get("domain", ""),
        "path": cookie.get("path", "/"),
        "secure": bool(cookie.get("secure")),
        "httpOnly": bool(cookie.get("httpOnly")),
        "expires": int(cookie.get("expires", -1) or -1),
        "hostOnly": not str(cookie.get("domain", "")).startswith("."),
    }


def har_to_capture(har, live_cookies=None):
    """Convert a recorded HAR to web2mcp-capture-v1: drop password-bearing requests and
    page assets, then overlay the live cookie snapshot (it includes HttpOnly cookies,
    which header reconstruction can miss)."""
    log = har.get("log") or {}
    entries = [e for e in (log.get("entries") or []) if not _entry_has_password(e) and _api_like(e)]
    capture = normalize_capture({"log": {**log, "entries": entries}})
    if live_cookies:
        captured_at = capture.get("captured_at") or datetime.now(timezone.utc).isoformat()
        origins = set()
        for ex in capture["exchanges"]:
            try:
                parts = urlsplit(ex["request"]["url"])
            except ValueError:
                continue
            if parts.scheme in ("http", "https") and parts.hostname:
                origins.add(f"{parts.scheme}://{parts.netloc}")
        sessions = capture.setdefault("sessions", {})
        for origin in origins:
            matching = [_cookie_record(c) for c in live_cookies if cookie_in_origin(c, origin)]
            if matching:
                sessions[origin] = {"captured_at": captured_at, "cookies": matching}
        capture["cookies"] = [_cookie_record(c) for c in live_cookies]
    return capture


def record(url, out, *, headless=False, browser_channel=None):
    """Open a Chromium window on ``url``, let the user work, and on window close write
    ``<out>/capture.json`` plus an inspect summary. Returns the summary (nothing sent)."""
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
        raise CallError("Supply an HTTP(S) URL without embedded credentials")
    root = Path(out).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise CallError(PLAYWRIGHT_HINT, reason="browser_unavailable") from None
    har_path = root / "record.har"
    profile = root / "browser-profile"  # keeps login state for a later re-record
    cookies = []
    print(
        "A Chromium window is opening. Log in and use the app as usual; "
        "CLOSE the window when you are done to finish recording.",
        file=sys.stderr,
        flush=True,
    )
    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                str(profile),
                headless=headless,
                channel=browser_channel,
                record_har_path=str(har_path),
                record_har_content="embed",
                viewport=None,
            )
        except PlaywrightError as error:
            if "install" in str(error).lower():
                raise CallError(
                    "Chromium is not installed for Playwright. Run: playwright install chromium",
                    reason="browser_unavailable",
                ) from None
            raise CallError(f"Could not launch Chromium: {error}", reason="browser_unavailable") from None
        try:
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(url, wait_until="commit", timeout=60000)
            except PlaywrightError:
                pass  # heavy SPAs may never settle; the user drives from here
            while True:
                try:
                    if not context.pages:
                        break
                    cookies = context.cookies()
                except PlaywrightError:
                    break  # window closed by the user
                time.sleep(1.0)
        finally:
            try:
                context.close()  # flushes the HAR
            except PlaywrightError:
                pass
    if not har_path.exists():
        raise CallError("Recording produced no HAR; was the window closed before any page loaded?")
    capture = har_to_capture(load_json(har_path), cookies)
    har_path.unlink()  # raw HAR may hold dropped password requests and page assets
    capture_path = root / "capture.json"
    save_private(capture_path, capture)
    summary = summarize_capture(capture_path)
    if summary.get("suggested_command"):
        summary["suggested_command"] = summary["suggested_command"].replace("python -m web2mcp", "web2mcp", 1)
    return {
        "capture": str(capture_path),
        "exchanges": len(capture["exchanges"]),
        "login_sessions": sorted(capture.get("sessions", {})),
        **summary,
        "next": "Review the origins above, then run the suggested discover command "
        "(--read-only-safe keeps every write hidden and blocked).",
    }
