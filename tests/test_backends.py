"""Backend selection, the no-backend guidance, and a Playwright exploration round."""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from web2mcp.backends import get_backend
from web2mcp.backends.ego import EgoBackend
from web2mcp.backends.playwright_backend import PlaywrightBackend
from web2mcp.client import CallError


def test_unknown_backend_name_is_rejected():
    with pytest.raises(CallError, match="Unknown browser backend"):
        get_backend("does-not-exist")


def test_no_backend_available_gives_actionable_guidance(monkeypatch):
    monkeypatch.setattr(EgoBackend, "available", lambda self: False)
    monkeypatch.setattr(PlaywrightBackend, "available", lambda self: False)
    with pytest.raises(CallError, match=r"web2mcp\[record\]|ego-lite|import a HAR"):
        get_backend()


def test_auto_selection_prefers_ego_then_playwright(monkeypatch):
    monkeypatch.setattr(EgoBackend, "available", lambda self: True)
    assert get_backend().name == "ego"
    monkeypatch.setattr(EgoBackend, "available", lambda self: False)
    monkeypatch.setattr(PlaywrightBackend, "available", lambda self: True)
    assert get_backend().name == "playwright"


def test_forced_backend_requires_availability(monkeypatch):
    monkeypatch.setattr(PlaywrightBackend, "available", lambda self: False)
    with pytest.raises(CallError, match="Playwright is not installed|remote-debugging"):
        get_backend("playwright")


# -- Playwright exploration round, end to end ---------------------------------

pw = pytest.importorskip("playwright.sync_api")

PAGE = b"""<!doctype html><html><head><script src="/static/app.js"></script></head>
<body><h1>lab</h1></body></html>"""
APP_JS = b"""fetch('/api/items').then(r=>r.json()).then(d=>{window.__items=d});"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/":
            body, ctype = PAGE, "text/html"
        elif self.path == "/static/app.js":
            body, ctype = APP_JS, "text/javascript"
        elif self.path.startswith("/api/items"):
            body, ctype = b'{"data":[{"id":"1"},{"id":"2"}]}', "application/json"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)


def _serve():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_playwright_backend_observe_round_captures_api_and_feeds_discover(tmp_path, monkeypatch):
    from web2mcp.discover import discover
    from web2mcp.explore import explore_round, source_map, start_session
    from web2mcp.storage import load_json

    monkeypatch.setenv("WEB2MCP_SETTLE_MS", "1500")
    backend = PlaywrightBackend()
    try:
        import playwright.sync_api as _pw

        with _pw.sync_playwright() as p:
            p.chromium.launch(headless=True).close()
    except Exception as error:
        pytest.skip(f"Chromium unavailable: {error}")

    server = _serve()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        start_session(tmp_path, origin + "/", "map the lab", [origin], backend="playwright")
        # source-map round: the Playwright backend lists the app's shipped JS.
        amap = source_map(tmp_path, backend=backend)
        assert any("/static/app.js" in u for u in amap["sources"])
        # observe round: navigation triggers the page's fetch('/api/items'), captured to capture.json.
        obs = explore_round(tmp_path, {"kind": "observe"}, backend=backend)
        assert obs["capture"]["total"] >= 1
        capture = load_json(tmp_path / "capture.json")
        assert any("/api/items" in ex["request"]["url"] for ex in capture["exchanges"])
        # The captured read flows through discover into a read-only profile.
        result = discover(tmp_path / "capture.json", tmp_path / "profile", [origin], read_only_safe=True)
        assert result["operations"] >= 1 and result["read_only"] >= 1
    finally:
        server.shutdown()
        server.server_close()
