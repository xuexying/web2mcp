"""`web2mcp import-openapi`: build a profile from an OpenAPI 3.x document.

For a product that already publishes an API spec, skip capture entirely — the value here
is adding web2mcp's write-safety layer on top of an API you already have. Each operation
becomes one or more synthetic exchanges (path parameters are instantiated with two differing
placeholder values so discover infers them as real parameters), and the whole synthetic
capture runs through the normal discover pipeline, so read/write classification and the
safety rules are identical. The spec carries no credentials: the profile's security schemes
are reported so you know what to add to the private credentials file before trialing.

Only OpenAPI 3.x JSON is supported; convert YAML to JSON first. Placeholder response shapes
come from the spec's schemas and are meant to be tightened by a later trial-read.
"""

import json
from pathlib import Path
from urllib.parse import urlsplit

from .client import CallError
from .discover import discover
from .storage import load_json, save_private

_METHODS = ("get", "post", "put", "patch", "delete", "head")


def _deref(spec, schema):
    seen = set()
    while isinstance(schema, dict) and "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str) or ref in seen or not ref.startswith("#/"):
            return {}
        seen.add(ref)
        node = spec
        for part in ref[2:].split("/"):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        schema = node
    return schema if isinstance(schema, dict) else {}


def _example(spec, schema, depth=0):
    schema = _deref(spec, schema)
    if not schema or depth > 6:
        return "example" if depth == 0 else None
    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    if schema.get("enum"):
        return schema["enum"][0]
    for key in ("allOf", "anyOf", "oneOf"):
        if schema.get(key):
            return _example(spec, schema[key][0], depth + 1)
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        props = schema.get("properties", {})
        required = schema.get("required") or list(props)[:4]
        return {k: _example(spec, props[k], depth + 1) for k in props if k in required}
    if kind == "array":
        return [_example(spec, schema.get("items", {}), depth + 1)]
    if kind in ("integer", "number"):
        return 1
    if kind == "boolean":
        return True
    return "example"


def _param_value(spec, param, n):
    schema = _deref(spec, param.get("schema", {}))
    kind = schema.get("type", "string")
    if schema.get("enum"):
        values = schema["enum"]
        return values[(n - 1) % len(values)]
    if kind in ("integer", "number"):
        return n
    return f"sample{n}"


def _json_schema(content):
    content = content or {}
    if "application/json" in content:
        return content["application/json"].get("schema")
    for ctype, media in content.items():
        if "json" in ctype:
            return media.get("schema")
    return None


def _params(spec, path_item, op):
    merged = []
    for p in list(path_item.get("parameters", [])) + list(op.get("parameters", [])):
        p = _deref(spec, p)
        if p.get("name") and p.get("in"):
            merged.append(p)
    return merged


def _synth_exchanges(spec, base, path, method, path_item, op):
    params = _params(spec, path_item, op)
    path_params = [p for p in params if p["in"] == "path"]
    query_params = [p for p in params if p["in"] == "query"]
    request_schema = _json_schema(_deref(spec, op.get("requestBody", {})).get("content"))
    body = _example(spec, request_schema) if request_schema is not None else None
    response_schema = None
    for code in ("200", "201", "2XX", "default"):
        if code in op.get("responses", {}):
            response_schema = _json_schema(_deref(spec, op["responses"][code]).get("content"))
            if response_schema is not None:
                break
    response_body = _example(spec, response_schema) if response_schema is not None else {}
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    # Two differing numeric path-parameter fills so discover infers them as parameters (its
    # identifier heuristic keys on id-shaped segments); one sample when there are no path params.
    fills = [{p["name"]: n for p in path_params} for n in (1, 2)] if path_params else [{}]
    exchanges = []
    for fill in fills:
        url = base + path
        for name, value in fill.items():
            url = url.replace("{" + name + "}", str(value))
        if query_params:
            qs = "&".join(f"{p['name']}={_param_value(spec, p, 1)}" for p in query_params)
            url += ("&" if "?" in url else "?") + qs
        exchanges.append(
            {
                "request": {
                    "method": method,
                    "url": url,
                    "headers": dict(headers),
                    "postData": json.dumps(body) if body is not None else None,
                },
                "response": {
                    "status": 200,
                    "mimeType": "application/json",
                    "body": json.dumps(response_body),
                },
                "label": f"OpenAPI {op.get('operationId') or method + ' ' + path}",
            }
        )
    return exchanges


def _base_url(spec, origins):
    if origins:
        return origins[0]
    servers = spec.get("servers") or []
    if servers and isinstance(servers[0], dict) and servers[0].get("url"):
        url = servers[0]["url"]
        if url.startswith(("http://", "https://")):
            return url.rstrip("/")
    raise CallError("No server URL in the spec; pass --origin https://api.example.com")


def import_openapi(spec_path, out, origins=None, read_only_safe=True):
    """Build a profile from an OpenAPI 3.x JSON document. Returns a summary; nothing is sent."""
    spec = load_json(spec_path)
    if not isinstance(spec, dict) or not isinstance(spec.get("paths"), dict):
        raise CallError("Not an OpenAPI document (no 'paths'). Only OpenAPI 3.x JSON is supported.")
    base = _base_url(spec, origins)
    exchanges = []
    for path, path_item in spec["paths"].items():
        if not isinstance(path_item, dict):
            continue
        for method in _METHODS:
            op = path_item.get(method)
            if isinstance(op, dict):
                exchanges += _synth_exchanges(spec, base, path, method.upper(), path_item, op)
    if not exchanges:
        raise CallError("No operations found under the OpenAPI 'paths'")
    root = Path(out)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    src = root / "openapi-source.json"
    save_private(src, {"format": "web2mcp-capture-v1", "exchanges": exchanges, "cookies": []})
    parts = urlsplit(base)
    chosen = origins or [f"{parts.scheme}://{parts.netloc}"]
    result = discover(src, root, chosen, read_only_safe=read_only_safe)
    schemes = sorted((spec.get("components", {}).get("securitySchemes") or {}).keys())
    return {
        "profile": result["profile"],
        "operations": result["operations"],
        "read_only": result["read_only"],
        "origins": chosen,
        "security_schemes": schemes,
        "next": "The spec carries no credentials. Add them to the profile's private "
        "credentials.json (the security_schemes above say what's needed), then trial-read reads. "
        "Placeholder response shapes are tightened by trial-read.",
    }
