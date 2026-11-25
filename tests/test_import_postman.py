"""`web2mcp import-postman`: build a profile from a Postman collection (v2.x)."""

import json

import pytest

from web2mcp.client import CallError
from web2mcp.import_spec import import_postman
from web2mcp.storage import load_json, save_private


def _collection():
    return {
        "info": {
            "name": "Demo",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "variable": [{"key": "baseUrl", "value": "https://api.demo.example"}],
        "item": [
            {
                "name": "Tasks",
                "item": [
                    {
                        "name": "List tasks",
                        "request": {
                            "method": "GET",
                            "header": [{"key": "Accept", "value": "application/json"}],
                            "url": {"raw": "{{baseUrl}}/api/tasks?status=open"},
                        },
                        "response": [
                            {
                                "name": "ok",
                                "status": "OK",
                                "code": 200,
                                "body": json.dumps({"data": [{"id": "1"}]}),
                            }
                        ],
                    },
                    {
                        "name": "Create task",
                        "request": {
                            "method": "POST",
                            "header": [{"key": "Content-Type", "value": "application/json"}],
                            "url": {"raw": "{{baseUrl}}/api/tasks"},
                            "body": {"mode": "raw", "raw": '{"title":"x"}'},
                        },
                        "response": [],
                    },
                ],
            }
        ],
    }


def test_import_postman_builds_profile_with_variable_and_folders(tmp_path):
    save_private(tmp_path / "coll.json", _collection())
    result = import_postman(tmp_path / "coll.json", tmp_path / "profile")
    assert result["origins"] == ["https://api.demo.example"]
    manifest = load_json(tmp_path / "profile" / "manifest.json")
    assert manifest["read_only_mode"] is True
    methods = sorted(op["method"] for op in manifest["operations"])
    assert "GET" in methods  # the read is published-eligible
    # The saved example response gave the read a real /data shape.
    get = next(op for op in manifest["operations"] if op["method"] == "GET")
    assert get["read_only"] is True
    # The POST is hidden by read-only-safe.
    post = next((op for op in manifest["operations"] if op["method"] == "POST"), None)
    assert post is not None and post.get("internal") is True


def test_import_postman_var_override_and_writes(tmp_path):
    coll = _collection()
    coll.pop("variable")  # force the caller to supply baseUrl
    save_private(tmp_path / "coll.json", coll)
    result = import_postman(
        tmp_path / "coll.json",
        tmp_path / "profile",
        variables={"baseUrl": "https://api.demo.example"},
        read_only_safe=False,
    )
    manifest = load_json(tmp_path / "profile" / "manifest.json")
    assert "read_only_mode" not in manifest
    assert any(op["method"] == "POST" and not op.get("internal") for op in manifest["operations"])
    assert result["operations"] >= 2


def test_import_postman_unresolved_variables_is_a_clear_error(tmp_path):
    coll = _collection()
    coll.pop("variable")  # baseUrl now unresolved
    save_private(tmp_path / "coll.json", coll)
    with pytest.raises(CallError, match="unresolved"):
        import_postman(tmp_path / "coll.json", tmp_path / "profile")


def test_import_postman_rejects_non_collection(tmp_path):
    save_private(tmp_path / "bad.json", {"paths": {}})
    with pytest.raises(CallError, match="Not a Postman collection"):
        import_postman(tmp_path / "bad.json", tmp_path / "profile")
