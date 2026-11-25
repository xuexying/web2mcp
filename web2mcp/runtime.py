"""Opt-in runtime health, identity-preserving login refresh and reviewed read trials."""

import copy
import tempfile
from pathlib import Path

from .client import Client, CallError
from .contracts import assert_checks, pointer
from .discover import discover, normalize_capture, captured_auth
from .storage_sessions import storage_auth
from .evidence import fingerprint
from .storage import load_json, save_private


def health(client, config):
    probe = config.get("identity_probe")
    if not probe:
        return {"healthy": False, "reason": "probe_not_configured"}
    if client.operations.get(probe["operation"], {}).get("read_only") is not True:
        raise CallError("Health probe must be a reviewed read-only operation")
    try:
        response = client.call(probe["operation"], probe.get("arguments", {}))
        if pointer(response, probe["pointer"]) != probe["expected"]:
            return {"healthy": False, "reason": "identity_mismatch"}
        return {"healthy": True, "reason": "ok", "automatic_replay": False}
    except CallError as error:
        return {"healthy": False, **error.as_dict()}


def refresh_credentials(profile, capture, *, transport=None):
    root = Path(profile)
    config = load_json(root / "runtime.json")
    if not config.get("identity_probe"):
        raise CallError("Configure a pinned identity probe before refreshing login")
    manifest = load_json(root / "manifest.json")
    before = fingerprint(manifest)
    fresh_capture = normalize_capture(load_json(capture))
    with tempfile.TemporaryDirectory(prefix="refresh-", dir=root) as temp:
        discover(capture, temp, manifest["allowed_origins"])
        fresh_manifest = load_json(Path(temp) / "manifest.json")
        fresh_credentials = load_json(Path(temp) / "credentials.json")
        credentials = copy.deepcopy(load_json(root / "credentials.json"))
        # Only map freshly observed endpoints back onto the SAME reviewed operations.
        changed = []
        for op in manifest["operations"]:

            def matches(candidate):
                if candidate["method"] != op["method"]:
                    return False
                a, b = candidate["url"].split("/"), op["url"].split("/")
                return len(a) == len(b) and all(
                    x == y or (i > 2 and (x.startswith("{") or y.startswith("{")))
                    for i, (x, y) in enumerate(zip(a, b))
                )

            candidates = [candidate for candidate in fresh_manifest["operations"] if matches(candidate)]
            if len(candidates) != 1:
                continue
            candidate = candidates[0]
            newref = candidate["credential_ref"]
            oldref = op["credential_ref"]
            oldauth = credentials["auth"].get(oldref, {})
            newauth = copy.deepcopy(fresh_credentials["auth"][newref])
            if oldauth.get("type") == "storage_session":

                def request_matches(request):
                    candidate = {"method": request["method"], "url": request["url"].split("?", 1)[0]}
                    return matches(candidate)

                samples = [e["request"] for e in fresh_capture["exchanges"] if request_matches(e["request"])]
                if not samples:
                    continue
                request = samples[-1]
                try:
                    _, headers = captured_auth(request, {})
                    newauth = storage_auth(request, fresh_capture, headers, oldauth["session"]["recipe"])
                    if newauth["session"]["source"] != oldauth["session"]["source"]:
                        raise ValueError("Storage source identity changed")
                except ValueError:
                    raise CallError(
                        "Fresh storage session failed source/signature checks",
                        reason="session_relay_required",
                    ) from None
            if newauth["type"] == "unsupported":
                continue
            if oldauth.get("type") == "cookie_session":
                if newauth.get("type") != "cookie_session":
                    continue
                # Refresh values/time, retain reviewed minimum cookies and age.
                for key in ("required_cookie_names", "max_age_seconds"):
                    newauth["session"][key] = oldauth["session"][key]
                # Do not introduce newly observed arbitrary request headers.
                selected = oldauth["session"]["header_names"]
                if not set(selected) <= set(newauth["session"]["headers"]):
                    continue
                newauth["session"]["headers"] = {k: newauth["session"]["headers"][k] for k in selected}
                newauth["session"]["header_names"] = selected
            credentials["auth"][oldref] = newauth
            credentials["headers"][oldref] = fresh_credentials["headers"][newref]
            changed.append(op["name"])
        if not changed:
            raise CallError("New capture contains no matching usable authentication")
        # Replace cookie jar, so stale session cookies are not silently carried over.
        credentials["cookies"] = fresh_credentials["cookies"]
        probe = config["identity_probe"]
        if probe["operation"] not in changed:
            raise CallError("New capture must include the identity probe")
        with Client(root, transport=transport, credentials=credentials) as candidate:
            result = health(candidate, config)
            if not result["healthy"]:
                raise CallError("Login refresh rejected: " + result["reason"], reason=result["reason"])
            if fingerprint(load_json(root / "manifest.json")) != before:
                raise CallError("Manifest changed during refresh")
            save_private(root / "credentials.json", credentials)
            candidate.audit.append(event="credentials_refreshed", operations=changed, manifest_hash=before)
        save_private(
            root / "cards" / "login.json", {"type": "login", "state": "resolved", "automatic_replay": False}
        )
        return {
            "refreshed": True,
            "operations": changed,
            "manifest_unchanged": True,
            "reconnect_required": True,
            "automatic_replay": False,
        }


def refresh_storage_credentials(profile, snapshot, *, transport=None):
    """Renew an existing storage source after user login; never change tools.

    Unlike capture-based refresh, this accepts only a new explicit storage
    snapshot. The old reviewed source, origins, header set and decoding recipe
    stay pinned; a live identity probe must succeed before atomic replacement.
    """
    root = Path(profile)
    config = load_json(root / "runtime.json")
    probe = config.get("identity_probe")
    if not probe:
        raise CallError("Configure a pinned identity probe before refreshing login")
    manifest = load_json(root / "manifest.json")
    before = fingerprint(manifest)
    fresh = load_json(snapshot)
    if fresh.get("format") != "web2mcp-storage-v1":
        raise CallError("Expected an explicit browser storage snapshot")
    credentials = copy.deepcopy(load_json(root / "credentials.json"))
    changed = []
    for ref, auth in credentials["auth"].items():
        if auth.get("type") != "storage_session":
            continue
        operation = next((o for o in manifest["operations"] if o["credential_ref"] == ref), None)
        if not operation:
            continue
        old = auth["session"]
        try:
            updated = storage_auth(
                {"method": operation["method"], "url": operation["url"]}, fresh, old["headers"], old["recipe"]
            )
            if updated["session"]["source"] != old["source"]:
                raise ValueError("Storage source changed")
        except (ValueError, KeyError):
            raise CallError(
                "Fresh storage session failed source/identity checks", reason="session_relay_required"
            ) from None
        credentials["auth"][ref] = updated
        changed.append(ref)
    probe_operation = next((o for o in manifest["operations"] if o["name"] == probe["operation"]), {})
    if probe_operation.get("credential_ref") not in changed:
        raise CallError("The identity probe must use the renewed storage source")
    with Client(root, credentials=credentials, transport=transport) as candidate:
        result = health(candidate, config)
        if not result["healthy"]:
            raise CallError("Storage refresh rejected: " + result["reason"], reason=result["reason"])
        if fingerprint(load_json(root / "manifest.json")) != before:
            raise CallError("Manifest changed during refresh")
        save_private(root / "credentials.json", credentials)
        candidate.audit.append(
            event="storage_credentials_refreshed", credential_refs=changed, manifest_hash=before
        )
    save_private(
        root / "cards" / "login.json", {"type": "login", "state": "resolved", "automatic_replay": False}
    )
    return {
        "refreshed": True,
        "credential_count": len(changed),
        "manifest_unchanged": True,
        "reconnect_required": True,
        "automatic_replay": False,
    }


def trial_read(client, operation, cases, *, reviewed_read_only=False):
    op = client.operations.get(operation)
    if not op or op.get("read_only") is not True or reviewed_read_only is not True:
        raise CallError("Trial needs explicit read-only classification review")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 20:
        raise CallError("Provide 1–20 bounded trial cases")
    if any(not case.get("assertions") for case in cases):
        raise CallError("Every trial must check business output")
    varying = bool(op.get("path_inference") or op["input_schema"].get("properties"))
    if varying and len({fingerprint(case.get("arguments", {})) for case in cases}) < 2:
        raise CallError("Parameterized tools require at least two different argument cases")
    evidence = []
    for case in cases:
        response = client.call(operation, case.get("arguments", {}))
        if response.get("schema_observation", {}).get("errors"):
            raise CallError("Response shape failed trial")
        assert_checks(response, case["assertions"])
        evidence.append(
            {
                "arguments_hmac": client.audit.arguments_hash(case.get("arguments", {})),
                "assertions": len(case["assertions"]),
                "response_fingerprint": fingerprint(response["data"]),
            }
        )
    record = {
        "type": "tool_trial",
        "operation": operation,
        "state": "passed",
        "operation_hash": fingerprint(op),
        "cases": evidence,
        "classification_reviewed": True,
    }
    save_private(client.root / "trials" / (operation + ".json"), record)
    return record


def publish_reads(client, names):
    manifest = copy.deepcopy(client.manifest)
    if not names or len(names) != len(set(names)):
        raise CallError("Choose unique trialed read tools")
    for name in names:
        op = client.operations.get(name)
        if not op or not op["read_only"]:
            raise CallError("Raw writes cannot be published by read trials; use a reviewed contract")
        path = client.root / "trials" / (name + ".json")
        if not path.exists():
            raise CallError("Tool has no passed trial")
        trial = load_json(path)
        if trial.get("state") != "passed" or trial["operation_hash"] != fingerprint(op):
            raise CallError("Trial is missing or stale")
        target = next(o for o in manifest["operations"] if o["name"] == name)
        target["internal"] = False
        target["verification"] = "read_trial_passed"
    save_private(client.root / "manifest.json", manifest)
    return {"published": names, "reconnect_required": True}


class Runtime:
    def __init__(self, client):
        self.client = client
        self.config = load_json(client.root / "runtime.json")

    def definitions(self):
        return [
            {
                "name": name,
                "description": description,
                "read_only": True,
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            }
            for name, description in [
                ("runtime_health", "Probe login and pinned identity without writes."),
                ("runtime_cards", "Read pending login/schema review cards; never auto-replay."),
                (
                    "runtime_audit_verify",
                    "Verify the local audit hash chain; not an external trust guarantee.",
                ),
            ]
        ]

    def call(self, name, arguments):
        if arguments:
            raise CallError("This runtime tool takes no arguments")
        if name == "runtime_health":
            return health(self.client, self.config)
        if name == "runtime_cards":
            return [load_json(p) for p in sorted((self.client.root / "cards").glob("*.json"))]
        if name == "runtime_audit_verify":
            return self.client.audit.verify()
        raise CallError("Unknown runtime tool")


def load_runtime(client):
    return Runtime(client) if (client.root / "runtime.json").exists() else None


def _path_matcher(template_path):
    import re

    parts = []
    for seg in re.split(r"(\{[^}]+\})", template_path):
        if seg.startswith("{") and seg.endswith("}"):
            parts.append(f"(?P<{seg[1:-1]}>[^/]+)")
        else:
            parts.append(re.escape(seg))
    return re.compile("^" + "".join(parts) + "$")


def _draft_assertions(body):
    import re

    checks = []
    if isinstance(body, dict):
        if "data" in body:
            checks.append({"pointer": "/data", "op": "exists", "value": True})
            inner = body["data"]
            if isinstance(inner, dict):
                for key, val in inner.items():
                    if isinstance(val, (str, int, float, bool)) and re.search(
                        r"(^|_)id$|name|status|code|title", key, re.I
                    ):
                        checks.append({"pointer": f"/data/{key}", "op": "exists", "value": True})
                        break
        else:
            for key, val in body.items():
                if isinstance(val, (str, int, float, bool)):
                    checks.append({"pointer": f"/{key}", "op": "exists", "value": True})
                    break
    elif isinstance(body, list) and body:
        checks.append({"pointer": "/0", "op": "exists", "value": True})
    return checks[:3]


def draft_read_cases(profile, capture_path):
    """Draft trial-read cases for every publishable read op, from the capture samples: real
    parameters pulled from the observed URLs and 'exists' assertions inferred from the observed
    response shape. Writes one read-cases/<op>.json per op in the profile, ready to review and
    pass to `trial-read`. Nothing is sent. A parameterized op with only one observed sample is
    flagged, since trial-read requires two differing cases."""
    import json
    from urllib.parse import parse_qs, urlsplit

    root = Path(profile)
    manifest = load_json(root / "manifest.json")
    capture = normalize_capture(load_json(capture_path))
    exchanges = capture.get("exchanges", [])
    out_dir = root / "read-cases"
    drafted = {}
    for op in manifest["operations"]:
        if op.get("read_only") is not True or op.get("internal"):
            continue
        target = urlsplit(op["url"])
        matcher = _path_matcher(target.path)
        parameterized = bool(op.get("path_inference") or op["input_schema"].get("properties"))
        cases, seen = [], set()
        for ex in exchanges:
            req = ex.get("request", {})
            if (req.get("method") or "").upper() != op["method"]:
                continue
            u = urlsplit(req.get("url", ""))
            if u.netloc != target.netloc or not matcher.match(u.path):
                continue
            args = {}
            groups = matcher.match(u.path).groupdict()
            if groups:
                args["path"] = groups
            query = {k: (v[0] if len(v) == 1 else v) for k, v in parse_qs(u.query).items()}
            if query and "query" in op["input_schema"].get("properties", {}):
                args["query"] = query
            key = fingerprint(args)
            if key in seen:
                continue
            try:
                body = json.loads(ex.get("response", {}).get("body") or "")
            except (ValueError, TypeError):
                continue
            checks = _draft_assertions(body)
            if not checks:
                continue
            seen.add(key)
            cases.append({"arguments": args, "assertions": checks})
        if not cases:
            continue
        save_private(out_dir / (op["name"] + ".json"), cases)
        entry = {"cases": len(cases), "file": str(out_dir / (op["name"] + ".json"))}
        if parameterized and len(cases) < 2:
            entry["note"] = "parameterized op: add a second differing case before trial-read"
        drafted[op["name"]] = entry
    return {
        "profile": str(root),
        "drafted": drafted,
        "next": "Review each read-cases/<op>.json (tighten 'exists' into exact value checks where "
        "you can), then: web2mcp trial-read <profile> <op> read-cases/<op>.json --reviewed-read-only",
    }
