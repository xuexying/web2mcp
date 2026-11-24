"""Regression tests for the pre-launch hardening: expanded redaction word list,
side-effect GET classification, synth read-only defaults, friendly tool names, and
torn/concurrent audit-key initialization."""

import json
from concurrent.futures import ThreadPoolExecutor

from test_v2 import ORIGIN, capture, exchange
from web2mcp.audit import Audit, redact
from web2mcp.discover import discover, op_name
from web2mcp.explore import auto_publish_reads, synthesize_profile
from web2mcp.storage import load_json, save_private


def test_audit_regenerates_a_torn_key_instead_of_crashing(tmp_path):
    # A partial (non-32-byte) fingerprint.key — as an interrupted or racing write could
    # leave — must not raise; it is regenerated to a valid 32-byte key.
    (tmp_path / "audit").mkdir()
    (tmp_path / "audit" / "fingerprint.key").write_bytes(b"short")
    audit = Audit(tmp_path)
    assert len(audit.key) == 32
    audit.append(event="ok")
    assert Audit(tmp_path).verify()["valid"] is True


def test_audit_concurrent_init_converges_on_one_key(tmp_path):
    def make(_):
        a = Audit(tmp_path)
        a.append(event="x")
        return a.key

    with ThreadPoolExecutor(max_workers=12) as pool:
        keys = list(pool.map(make, range(48)))
    assert len(set(keys)) == 1
    assert Audit(tmp_path).verify()["records"] == 48


def test_redact_masks_expanded_sensitive_words():
    value = {
        "api_key": "k1",
        "apiKey": "k2",
        "signature": "s",
        "x-credential": "c",
        "session_id": "v",
        "note": "keep",
    }
    out = redact(value)
    assert out["note"] == "keep"
    assert all(out[key] == "[REDACTED]" for key in value if key != "note")


def test_default_tool_names_are_friendly():
    taken = set()
    assert op_name("GET", "/api/v1/records", taken) == "list_records"
    assert op_name("GET", "/api/v1/records/{record_id}", taken) == "get_record"
    assert op_name("POST", "/api/v1/records", taken) == "post_records"
    assert op_name("GET", "/api/records/{id}/items", taken) == "list_records_items"
    # Only a real collision gets a suffix.
    assert op_name("GET", "/api/v2/records", taken) == "list_records_2"


def test_discover_does_not_mark_side_effect_get_read_only(tmp_path):
    cap = capture()
    cap["exchanges"].append(exchange("GET", "/api/records/101/export", {"ok": True}))
    save_private(tmp_path / "cap.json", cap)
    discover(tmp_path / "cap.json", tmp_path / "profile", [ORIGIN])
    manifest = load_json(tmp_path / "profile" / "manifest.json")
    export = next(op for op in manifest["operations"] if "export" in op["url"])
    assert export["read_only"] is False


def _session(tmp_path):
    save_private(
        tmp_path / "session.json",
        {
            "version": 1,
            "id": "fixture",
            "url": ORIGIN + "/app",
            "goal": "inspect",
            "origins": [ORIGIN],
            "budget": 10,
            "actions_used": 0,
            "round": 0,
            "stage": "created",
            "space_id": None,
        },
    )


def test_synth_profile_defaults_to_read_only_mode(tmp_path):
    _session(tmp_path)
    save_private(
        tmp_path / "source_api_map.json",
        {
            "endpoints": [
                {"method": "GET", "path": "/api/records", "kind": "read"},
                {"method": "GET", "path": "/api/records/export", "kind": "read"},
                {"method": "POST", "path": "/api/records", "kind": "write"},
            ]
        },
    )
    result = synthesize_profile(tmp_path, api_origin=ORIGIN)
    manifest = load_json(tmp_path / "synth" / "manifest.json")
    assert manifest["read_only_mode"] is True
    assert manifest["allowed_methods"] == ["GET", "HEAD"]
    export = next(op for op in manifest["operations"] if "export" in op["url"])
    # A GET on a side-effect path is not a read, and every non-read is hidden by default.
    assert export["read_only"] is False
    assert all(op.get("internal") for op in manifest["operations"] if not op["read_only"])
    assert result["read"] == 1 and result["write"] == 2


def test_auto_publish_skips_static_only_reads(tmp_path):
    _session(tmp_path)
    save_private(
        tmp_path / "source_api_map.json",
        {
            "endpoints": [
                {"method": "GET", "path": "/api/static/items", "kind": "read"},
            ]
        },
    )
    save_private(
        tmp_path / "capture.json",
        {
            "format": "web2mcp-capture-v1",
            "exchanges": [
                {
                    "request": {"method": "GET", "url": ORIGIN + "/api/records", "headers": {}},
                    "response": {"status": 200, "body": json.dumps({"data": []})},
                }
            ],
        },
    )
    synthesize_profile(tmp_path, api_origin=ORIGIN)
    result = auto_publish_reads(tmp_path)
    # The statically-inferred read is never auto-fired; only the runtime-captured one is trialed.
    assert result["skipped_static_only"] == 1
    assert result["read_ops"] == 1
    assert all("static" not in failure["name"] for failure in result["failed"])
