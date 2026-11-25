"""`web2mcp import-openapi`: build a profile from an OpenAPI 3.x JSON document."""

import pytest

from web2mcp.client import CallError
from web2mcp.import_spec import import_openapi
from web2mcp.storage import load_json, save_private

SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Demo", "version": "1.0"},
    "servers": [{"url": "https://api.demo.example/v1"}],
    "components": {
        "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}},
        "schemas": {
            "Task": {
                "type": "object",
                "required": ["id", "title", "status"],
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "status": {"type": "string", "enum": ["open", "done"]},
                },
            }
        },
    },
    "paths": {
        "/tasks": {
            "get": {
                "operationId": "listTasks",
                "parameters": [{"name": "status", "in": "query", "schema": {"type": "string"}}],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"type": "array", "items": {"$ref": "#/components/schemas/Task"}}
                            }
                        }
                    }
                },
            },
            "post": {
                "operationId": "createTask",
                "requestBody": {
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Task"}}}
                },
                "responses": {
                    "201": {
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Task"}}}
                    }
                },
            },
        },
        "/tasks/{taskId}": {
            "get": {
                "operationId": "getTask",
                "parameters": [
                    {"name": "taskId", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {
                    "200": {
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Task"}}}
                    }
                },
            }
        },
    },
}


def test_import_openapi_builds_read_only_profile_with_parameters(tmp_path):
    save_private(tmp_path / "spec.json", SPEC)
    result = import_openapi(tmp_path / "spec.json", tmp_path / "profile")
    assert result["origins"] == ["https://api.demo.example"]
    assert result["security_schemes"] == ["bearerAuth"]
    manifest = load_json(tmp_path / "profile" / "manifest.json")
    assert manifest["read_only_mode"] is True
    # The /tasks/{taskId} path parameter was inferred from the two synthetic samples
    # (discover names it from the collection segment, e.g. task_id).
    get_task = next(op for op in manifest["operations"] if op.get("path_inference"))
    assert "{" in get_task["url"] and get_task["read_only"] is True
    # listTasks (collection GET) and getTask (item GET) are both reads.
    assert sum(1 for op in manifest["operations"] if op["read_only"]) >= 2
    # The write (POST /tasks) is present but hidden by read-only-safe.
    post = next(op for op in manifest["operations"] if op["method"] == "POST")
    assert post["read_only"] is False and post.get("internal") is True


def test_import_openapi_writes_mode_exposes_writes(tmp_path):
    save_private(tmp_path / "spec.json", SPEC)
    result = import_openapi(tmp_path / "spec.json", tmp_path / "profile", read_only_safe=False)
    manifest = load_json(tmp_path / "profile" / "manifest.json")
    assert "read_only_mode" not in manifest
    post = next(op for op in manifest["operations"] if op["method"] == "POST")
    assert not post.get("internal")
    assert result["operations"] >= 3


def test_import_openapi_origin_override(tmp_path):
    spec = {k: v for k, v in SPEC.items() if k != "servers"}
    save_private(tmp_path / "spec.json", spec)
    result = import_openapi(
        tmp_path / "spec.json", tmp_path / "profile", origins=["https://staging.demo.example"]
    )
    assert result["origins"] == ["https://staging.demo.example"]


def test_import_openapi_rejects_non_openapi(tmp_path):
    save_private(tmp_path / "bad.json", {"not": "openapi"})
    with pytest.raises(CallError, match="Not an OpenAPI"):
        import_openapi(tmp_path / "bad.json", tmp_path / "profile")


def test_import_openapi_without_server_needs_origin(tmp_path):
    spec = {k: v for k, v in SPEC.items() if k != "servers"}
    save_private(tmp_path / "spec.json", spec)
    with pytest.raises(CallError, match="No server URL"):
        import_openapi(tmp_path / "spec.json", tmp_path / "profile")
