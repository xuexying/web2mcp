"""`web2mcp draft-read-cases`: generate trial-read case drafts from capture samples (P1-4)."""

from test_v2 import ORIGIN, capture, exchange
from web2mcp.discover import discover
from web2mcp.runtime import draft_read_cases
from web2mcp.storage import load_json, save_private


def test_draft_generates_parameterized_and_simple_read_cases(tmp_path):
    save_private(tmp_path / "cap.json", capture())
    discover(tmp_path / "cap.json", tmp_path / "profile", [ORIGIN])
    result = draft_read_cases(tmp_path / "profile", tmp_path / "cap.json")
    drafted = result["drafted"]
    # The parameterized detail read (GET /api/records/{record_id}) draws >=2 differing cases.
    assert drafted["get_record"]["cases"] >= 2
    assert "note" not in drafted["get_record"]
    # The non-parameterized identity read draws one.
    assert "get_me" in drafted
    cases = load_json(tmp_path / "profile" / "read-cases" / "get_record.json")
    assert all(c["assertions"] for c in cases)
    assert all("path" in c["arguments"] for c in cases)
    # Each case's path argument is a concrete observed id.
    ids = {c["arguments"]["path"]["record_id"] for c in cases}
    assert {"101", "102"}.issubset(ids)


def test_draft_skips_writes_and_handles_a_single_literal_read(tmp_path):
    cap = capture()
    # A single literal-path read (one sample, so discover keeps the path literal).
    cap["exchanges"].append(exchange("GET", "/api/widgets/55", {"id": "55", "name": "w"}))
    save_private(tmp_path / "cap.json", cap)
    discover(tmp_path / "cap.json", tmp_path / "profile", [ORIGIN])
    result = draft_read_cases(tmp_path / "profile", tmp_path / "cap.json")
    # The write op (POST decision) never appears in read-case drafts.
    assert not any("decision" in name for name in result["drafted"])
    widget = result["drafted"].get("get_widget")
    assert widget and widget["cases"] == 1
