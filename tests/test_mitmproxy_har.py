"""mitmproxy exports standard HAR 1.2, so the existing HAR import consumes it directly —
covering phone apps and desktop/Electron apps proxied through mitmproxy. This pins the
shapes mitmproxy emits that differ from a browser DevTools HAR: base64-encoded response
bodies, structured request `cookies[]`/`queryString[]`, and an HTTP/2 httpVersion."""

import base64
import json

from web2mcp.discover import discover, normalize_capture, quick, summarize_capture
from web2mcp.storage import load_json, save_private

ORIGIN = "https://api.app.example"


def _mitmproxy_har():
    body = json.dumps({"data": [{"id": "1"}, {"id": "2"}]})
    b64 = base64.b64encode(body.encode()).decode()
    return {
        "log": {
            "version": "1.2",
            "creator": {"name": "mitmproxy", "version": "11.0.0"},
            "entries": [
                {
                    "startedDateTime": "2026-11-20T10:00:00.000000+00:00",
                    "request": {
                        "method": "GET",
                        "url": ORIGIN + "/api/items?page=1",
                        "httpVersion": "HTTP/2.0",
                        "headers": [
                            {"name": "Cookie", "value": "sid=abc123"},
                            {"name": "Accept", "value": "application/json"},
                        ],
                        "queryString": [{"name": "page", "value": "1"}],
                        "cookies": [{"name": "sid", "value": "abc123"}],
                        "headersSize": -1,
                        "bodySize": 0,
                    },
                    "response": {
                        "status": 200,
                        "httpVersion": "HTTP/2.0",
                        "headers": [{"name": "content-type", "value": "application/json"}],
                        "cookies": [],
                        "content": {
                            "size": len(body),
                            "mimeType": "application/json",
                            "text": b64,
                            "encoding": "base64",
                        },
                        "redirectURL": "",
                        "headersSize": -1,
                        "bodySize": len(body),
                    },
                    "cache": {},
                    "timings": {"send": 0, "wait": 5, "receive": 1},
                }
            ],
        }
    }


def test_mitmproxy_har_normalizes_with_decoded_body_and_login(tmp_path):
    capture = normalize_capture(_mitmproxy_har())
    ex = capture["exchanges"][0]
    # The base64 response body is decoded back to JSON text.
    assert json.loads(ex["response"]["body"])["data"][0]["id"] == "1"
    # The request cookie is reconstructed into an origin session (login travels with the HAR).
    assert ORIGIN in capture["sessions"]
    assert any(c["name"] == "sid" for c in capture["sessions"][ORIGIN]["cookies"])


def test_mitmproxy_har_flows_through_quick(tmp_path):
    save_private(tmp_path / "mobile.har", _mitmproxy_har())
    summary = summarize_capture(tmp_path / "mobile.har")
    assert ORIGIN in summary["suggested_origins"]
    result = quick(tmp_path / "mobile.har")
    assert result["origins"] == [ORIGIN]
    assert result["operations"] >= 1 and result["read_only"] >= 1
    manifest = load_json(result["profile"] + "/manifest.json")
    assert manifest["read_only_mode"] is True


def test_mitmproxy_har_discovers_a_read_only_operation(tmp_path):
    save_private(tmp_path / "mobile.har", _mitmproxy_har())
    result = discover(tmp_path / "mobile.har", tmp_path / "profile", [ORIGIN], read_only_safe=True)
    assert result["operations"] == 1 and result["read_only"] == 1
