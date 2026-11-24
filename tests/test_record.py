"""`web2mcp record`: password-bearing requests never persist; HAR converts to a
capture with an authoritative live-cookie session snapshot; Playwright stays optional."""

import json

import pytest

from web2mcp.client import CallError
from web2mcp.record import cookie_in_origin, har_to_capture, has_password_input

ORIGIN = "https://app.example"


def entry(
    method="GET",
    path="/api/items",
    *,
    mime="application/json",
    body='{"data":[]}',
    post=None,
    headers=None,
    url=None,
):
    return {
        "startedDateTime": "2026-11-20T10:00:00.000Z",
        "request": {
            "method": method,
            "url": url or ORIGIN + path,
            "headers": [{"name": k, "value": v} for k, v in (headers or {}).items()],
            **({"postData": {"text": post}} if post is not None else {"postData": None}),
        },
        "response": {
            "status": 200,
            "headers": [],
            "content": {"mimeType": mime, "text": body},
        },
    }


def har(*entries):
    return {"log": {"version": "1.2", "entries": list(entries)}}


@pytest.mark.parametrize(
    "url,headers,post",
    [
        (ORIGIN + "/login", {}, '{"user":"a","password":"hunter2"}'),
        (ORIGIN + "/login", {}, '{"nested":{"credentials":{"passwd":"x"}}}'),
        (ORIGIN + "/login", {}, "user=a&password=hunter2"),
        (ORIGIN + "/verify?otp=123456", {}, None),
        (ORIGIN + "/api", {"Authorization": "Basic dXNlcjpwYXNz"}, None),
        (ORIGIN + "/api", {"X-Password": "x"}, None),
        (ORIGIN + "/form", {}, '<input name="password" value="x">'),
    ],
)
def test_password_material_is_detected(url, headers, post):
    assert has_password_input(url, headers, post) is True


def test_ordinary_authenticated_request_is_not_flagged():
    assert (
        has_password_input(
            ORIGIN + "/api/items?page=2",
            {"Authorization": "Bearer token-abc", "Cookie": "sid=1"},
            '{"query":"laptops"}',
        )
        is False
    )


def test_har_to_capture_drops_password_requests_and_page_assets():
    capture = har_to_capture(
        har(
            entry(),
            entry("POST", "/login", post='{"user":"a","password":"hunter2"}'),
            entry("GET", "/app.js", mime="text/javascript", body="var x=1"),
            entry("GET", "/logo.png", mime="image/png", body=""),
            entry("POST", "/api/search", post='{"q":"x"}'),
        )
    )
    urls = [ex["request"]["url"] for ex in capture["exchanges"]]
    assert ORIGIN + "/api/items" in urls and ORIGIN + "/api/search" in urls
    assert len(urls) == 2  # login, js and png are gone
    assert "hunter2" not in json.dumps(capture)


def test_live_cookie_snapshot_becomes_origin_scoped_sessions():
    live = [
        {
            "name": "sid",
            "value": "s1",
            "domain": "app.example",
            "path": "/",
            "expires": -1,
            "httpOnly": True,
            "secure": True,
        },
        {
            "name": "other",
            "value": "x",
            "domain": "unrelated.example",
            "path": "/",
            "expires": -1,
            "httpOnly": False,
            "secure": True,
        },
    ]
    capture = har_to_capture(har(entry()), live)
    session = capture["sessions"][ORIGIN]
    names = [c["name"] for c in session["cookies"]]
    assert names == ["sid"]  # HttpOnly captured; foreign origin excluded
    assert session["cookies"][0]["hostOnly"] is True


def test_cookie_origin_scoping_matches_subdomains_only_for_dot_domains():
    assert cookie_in_origin({"domain": ".example.com"}, "https://api.example.com")
    assert cookie_in_origin({"domain": "api.example.com"}, "https://api.example.com")
    assert not cookie_in_origin({"domain": "api.example.com"}, "https://www.example.com")
    assert not cookie_in_origin({"domain": ".example.com"}, "https://evil-example.com")


def test_record_without_playwright_points_to_har_path(tmp_path, monkeypatch):
    import sys as _sys

    from web2mcp import record as record_module

    monkeypatch.setitem(_sys.modules, "playwright", None)
    monkeypatch.setitem(_sys.modules, "playwright.sync_api", None)
    with pytest.raises(CallError, match=r"web2mcp\[record\]"):
        record_module.record("https://app.example", tmp_path / "out")
