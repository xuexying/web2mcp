"""`web2mcp quick`: capture/HAR -> auto-picked origin -> read-only-safe profile + config."""

import json
from pathlib import Path

import pytest

from test_v2 import ORIGIN, capture
from web2mcp.client import CallError
from web2mcp.discover import quick
from web2mcp.storage import load_json, save_private


def test_quick_single_origin_auto_builds_read_only_profile(tmp_path):
    save_private(tmp_path / "cap.json", capture())
    result = quick(tmp_path / "cap.json")
    assert result["origins"] == [ORIGIN]
    assert result["operations"] >= 1 and result["read_only"] >= 1
    manifest = load_json(Path(result["profile"]) / "manifest.json")
    assert manifest["read_only_mode"] is True
    # The returned config is paste-ready.
    assert "mcpServers" in result["mcp_config"]
    assert "web2mcp" in json.dumps(result["mcp_config"])


def test_quick_several_candidates_stops_and_lists_them(tmp_path):
    cap = capture()
    other = "https://api.other.example"
    cap["exchanges"].append(
        {
            "request": {
                "method": "GET",
                "url": other + "/api/items",
                "headers": {"Authorization": "Bearer x", "Content-Type": "application/json"},
                "postData": None,
            },
            "response": {"status": 200, "body": json.dumps({"data": [1]})},
            "label": "other origin",
        }
    )
    save_private(tmp_path / "cap.json", cap)
    with pytest.raises(CallError, match="Several candidate origins"):
        quick(tmp_path / "cap.json")


def test_quick_origin_override_builds_without_stopping(tmp_path):
    cap = capture()
    other = "https://api.other.example"
    cap["exchanges"].append(
        {
            "request": {
                "method": "GET",
                "url": other + "/api/items",
                "headers": {"Authorization": "Bearer x", "Content-Type": "application/json"},
                "postData": None,
            },
            "response": {"status": 200, "body": json.dumps({"data": [1]})},
            "label": "other origin",
        }
    )
    save_private(tmp_path / "cap.json", cap)
    result = quick(tmp_path / "cap.json", origins=[ORIGIN])
    assert result["origins"] == [ORIGIN]
    assert result["operations"] >= 1


def test_quick_no_api_origin_is_a_clear_error(tmp_path):
    # Only a non-JSON response: no origin qualifies as an API surface.
    save_private(
        tmp_path / "cap.json",
        {
            "format": "web2mcp-capture-v1",
            "exchanges": [
                {
                    "request": {"method": "GET", "url": ORIGIN + "/page", "headers": {}, "postData": None},
                    "response": {"status": 200, "mimeType": "text/html", "body": "<html></html>"},
                    "label": "page",
                }
            ],
            "cookies": [],
        },
    )
    with pytest.raises(CallError, match="No clear API origin"):
        quick(tmp_path / "cap.json")
