"""`web2mcp add-curl`: parse a bash 'Copy as cURL' into one profile operation."""

import pytest

from web2mcp.add_curl import add_curl, parse_curl
from web2mcp.client import CallError
from web2mcp.storage import load_json

API = "https://api.example.com"


def test_parse_simple_get_with_headers():
    method, url, headers, data = parse_curl(f"curl '{API}/api/items?page=2' -H 'Accept: application/json'")
    assert method == "GET" and url == f"{API}/api/items?page=2"
    assert headers["Accept"] == "application/json" and data is None


def test_parse_post_json_infers_method_and_content_type():
    method, url, headers, data = parse_curl(f"""curl '{API}/api/orders' --data-raw '{{"item":"x"}}'""")
    assert method == "POST"
    assert headers["Content-Type"] == "application/json"
    assert data == '{"item":"x"}'


def test_parse_handles_line_continuations_cookies_and_explicit_method():
    method, url, headers, data = parse_curl(
        f"""curl '{API}/api/me' \\
        -X GET \\
        -b 'sid=abc; theme=dark' \\
        -H 'Authorization: Bearer tok'"""
    )
    assert method == "GET" and headers["Cookie"] == "sid=abc; theme=dark"
    assert headers["Authorization"] == "Bearer tok"


def test_parse_get_flag_moves_data_to_query():
    method, url, headers, data = parse_curl(f"curl -G '{API}/api/search' --data 'q=laptops'")
    assert method == "GET" and url.endswith("?q=laptops") and data is None


def test_parse_user_becomes_basic_auth():
    _, _, headers, _ = parse_curl(f"curl '{API}/api/me' -u alice:secret")
    assert headers["Authorization"].startswith("Basic ")


def test_parse_rejects_missing_or_relative_url():
    with pytest.raises(CallError, match="No URL"):
        parse_curl("curl -H 'Accept: application/json'")
    with pytest.raises(CallError, match="absolute http"):
        parse_curl("curl '/api/items'")


def test_add_curl_builds_a_read_operation(tmp_path):
    result = add_curl(tmp_path / "profile", f"curl '{API}/api/items' -H 'Accept: application/json'")
    assert result["operations"] == 1 and result["read_only"] == 1
    assert result["origins"] == [API]
    manifest = load_json(tmp_path / "profile" / "manifest.json")
    assert manifest["operations"][0]["method"] == "GET"


def test_add_curl_accumulates_multiple_operations(tmp_path):
    prof = tmp_path / "profile"
    add_curl(prof, f"curl '{API}/api/items'")
    result = add_curl(prof, f"""curl '{API}/api/orders' --data-raw '{{"item":"x"}}'""")
    # Both the GET and the POST are now in one profile.
    assert result["operations"] == 2
    manifest = load_json(prof / "manifest.json")
    methods = sorted(op["method"] for op in manifest["operations"])
    assert methods == ["GET", "POST"]


def test_add_curl_read_only_safe_hides_writes(tmp_path):
    prof = tmp_path / "profile"
    result = add_curl(prof, f"""curl '{API}/api/orders' --data-raw '{{"item":"x"}}'""", read_only_safe=True)
    manifest = load_json(prof / "manifest.json")
    assert manifest["read_only_mode"] is True
    assert all(op.get("internal") for op in manifest["operations"] if not op["read_only"])
    assert result["operations"] == 1
