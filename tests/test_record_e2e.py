"""End-to-end recorder check against the local lab server, using real Playwright.

Runs only where Playwright (and its Chromium) is installed — `pip install
'web2mcp[record]' && playwright install chromium`; skipped otherwise (CI included).
"""

import sys
import threading
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from lab_product import make_server  # noqa: E402

from web2mcp.discover import discover, summarize_capture  # noqa: E402
from web2mcp.record import har_to_capture  # noqa: E402
from web2mcp.storage import load_json, save_private  # noqa: E402


def test_recorded_browser_traffic_reaches_a_read_only_profile(tmp_path):
    server, state = make_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    har_path = tmp_path / "record.har"
    try:
        with pw.sync_playwright() as p:
            try:
                context = p.chromium.launch_persistent_context(
                    str(tmp_path / "profile"),
                    headless=True,
                    record_har_path=str(har_path),
                    record_har_content="embed",
                )
            except pw.Error as error:
                pytest.skip(f"Chromium unavailable: {error}")
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(origin + "/api/records/101", wait_until="commit")
            page.evaluate(
                """async (origin) => {
                    await fetch(origin + '/api/login', {method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({username: 'demo', password: 'demo'})});
                    await (await fetch(origin + '/api/me')).text();
                    await (await fetch(origin + '/api/records/102')).text();
                }""",
                origin,
            )
            # Mirror record()'s poll loop: give Playwright a moment to flush response
            # bodies into the HAR before the context is torn down.
            page.wait_for_timeout(1000)
            cookies = context.cookies()
            context.close()
        capture = har_to_capture(load_json(har_path), cookies)
        # The login request carried a password: it must not survive into the capture.
        assert all("/api/login" not in ex["request"]["url"] for ex in capture["exchanges"])
        assert not any("password" in (ex["request"].get("postData") or "") for ex in capture["exchanges"])
        urls = {ex["request"]["url"] for ex in capture["exchanges"]}
        assert origin + "/api/me" in urls and origin + "/api/records/102" in urls
        # The live cookie snapshot carries the login forward (no browser at call time).
        assert origin in capture.get("sessions", {})
        save_private(tmp_path / "capture.json", capture)
        summary = summarize_capture(tmp_path / "capture.json")
        assert origin in summary["suggested_origins"]
        result = discover(tmp_path / "capture.json", tmp_path / "profile-out", [origin], read_only_safe=True)
        assert result["operations"] >= 2 and result["read_only"] >= 2
        manifest = load_json(tmp_path / "profile-out" / "manifest.json")
        assert manifest["read_only_mode"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
