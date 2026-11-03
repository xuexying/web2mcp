"""Infer only observed HTTP operations. Never claims complete API coverage."""
import base64
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from .storage import load_json, save_private
from .evidence import infer_schema, group_exchanges, fingerprint
from .sessions import session_auth
from .storage_sessions import storage_auth


def schema(value):
    return infer_schema([value])


def response_payload(sample):
    # An observed empty response is distinct from a body that capture failed
    # to obtain. Match Client's representation; only reviewed writes opt in.
    response = sample['response']
    if response.get('body') == '' and not response.get('unavailable'):
        return ''
    return json.loads(response.get('body') or '')


def sensitive_body(value):
    if isinstance(value, dict):
        return any(re.search(r'password|secret|access_token|refresh_token|mac_key|uckey', key, re.I)
                   or sensitive_body(item) for key,item in value.items())
    if isinstance(value, list): return any(sensitive_body(item) for item in value)
    return False


def captured_auth(request, tokens):
    headers = {k.lower(): v for k, v in request.get('headers', {}).items()}
    auth_text = headers.pop('authorization', '')
    if auth_text and not auth_text.startswith('Bearer '):
        auth = {'type': 'unsupported'}
    else:
        auth = {'type': 'static', 'value': auth_text} if auth_text else {'type': 'none'}
    return auth, headers




def request_parts(request):
    headers = {k.lower(): v for k, v in request.get('headers', {}).items()}
    query = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(urlsplit(request['url']).query, keep_blank_values=True).items()}
    body = None; body_type = None
    if request.get('postData'):
        if 'application/json' in headers.get('content-type', ''):
            body = json.loads(request['postData']); body_type = 'json'
        elif 'application/x-www-form-urlencoded' in headers.get('content-type', ''):
            body = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(request['postData'], keep_blank_values=True).items()}; body_type = 'form'
        else: raise ValueError('Unsupported request body')
    return query, body, body_type


def scoped_cookies(cookies, urls):
    result = []
    for cookie in cookies:
        domain = cookie['domain'].lstrip('.').lower()
        for url in urls:
            parsed = urlsplit(url); host = parsed.hostname or ''
            domain_match = host == domain or cookie['domain'].startswith('.') and host.endswith('.' + domain)
            path = cookie.get('path', '/')
            path_match = parsed.path == path or parsed.path.startswith(path if path.endswith('/') else path + '/')
            if domain_match and path_match and (not cookie.get('secure') or parsed.scheme == 'https'):
                result.append(cookie); break
    return result


def _har_expires_to_epoch(expires):
    """HAR Set-Cookie expires is ISO 8601; map to epoch seconds, -1 for session cookies."""
    if expires in (None, "", -1, "-1"):
        return -1
    if isinstance(expires, (int, float)):
        return int(expires)
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
        return int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp())
    except ValueError:
        return -1


def normalize_capture(value):
    if value.get("format") == "web2mcp-capture-v1":
        return value
    if "log" not in value:
        raise ValueError("Expected web2mcp capture or HAR 1.2")
    from http.cookies import SimpleCookie
    exchanges = []
    # Reconstruct a per-origin cookie snapshot from the HAR so logged-in sites work from an
    # imported HAR, not only from live browser capture. The authoritative current value is what
    # the browser actually SENT (request Cookie header / request.cookies); response Set-Cookie
    # only enriches metadata (domain/path/expires/secure) and seeds set-but-not-yet-sent cookies.
    origin_jars = {}   # origin -> {name: cookie record}
    origin_time = {}   # origin -> latest startedDateTime (ISO, carries tz)
    latest_overall = None
    for entry in value["log"]["entries"]:
        req, res = entry["request"], entry["response"]
        content = res.get("content", {})
        body = content.get("text")
        if body and content.get("encoding") == "base64":
            body = base64.b64decode(body).decode("utf-8", errors="replace")
        req_headers = {h["name"]: h["value"] for h in req.get("headers", [])}
        exchanges.append({"request": {"method": req["method"], "url": req["url"],
            "headers": req_headers,
            "postData": req.get("postData", {}).get("text")},
            "response": {"status": res["status"], "mimeType": content.get("mimeType", ""), "body": body},
            "label": entry.get("comment", "Imported HAR")})
        try:
            parts = urlsplit(req["url"])
        except ValueError:
            continue
        if parts.scheme not in ("http", "https") or not parts.hostname:
            continue
        origin = f"{parts.scheme}://{parts.netloc}"
        host, secure = parts.hostname, parts.scheme == "https"
        started = entry.get("startedDateTime")
        if started:
            if started > origin_time.get(origin, ""):
                origin_time[origin] = started
            if latest_overall is None or started > latest_overall:
                latest_overall = started
        jar = origin_jars.setdefault(origin, {})
        # Sent cookies: prefer the structured request.cookies, else parse the Cookie header.
        sent = {c["name"]: c.get("value", "") for c in req.get("cookies", []) or [] if c.get("name")}
        if not sent:
            raw = req_headers.get("Cookie") or req_headers.get("cookie")
            if raw:
                jar_cookie = SimpleCookie()
                try:
                    jar_cookie.load(raw)
                    sent = {k: m.value for k, m in jar_cookie.items()}
                except Exception:
                    sent = {}
        for name, val in sent.items():
            record = jar.setdefault(name, {"name": name, "value": val, "domain": host,
                "hostOnly": True, "path": "/", "secure": secure, "expires": -1})
            record["value"] = val  # last value the browser actually sent wins
        # Set-Cookie: enrich metadata; seed cookies set but not (yet) seen in a request.
        for c in res.get("cookies", []) or []:
            name = c.get("name")
            if not name:
                continue
            record = jar.setdefault(name, {"name": name, "value": c.get("value", ""),
                "domain": host, "hostOnly": True, "path": "/", "secure": secure, "expires": -1})
            domain = c.get("domain")
            if domain:
                record["domain"], record["hostOnly"] = domain, not str(domain).startswith(".")
            if c.get("path"):
                record["path"] = c["path"]
            if "secure" in c:
                record["secure"] = bool(c["secure"])
            if "httpOnly" in c:
                record["httpOnly"] = bool(c["httpOnly"])
            if c.get("expires"):
                record["expires"] = _har_expires_to_epoch(c.get("expires"))
    sessions = {origin: {"captured_at": origin_time.get(origin) or latest_overall,
                         "cookies": list(jar.values())}
                for origin, jar in origin_jars.items() if jar}
    out = {"format": "web2mcp-capture-v1", "exchanges": exchanges,
           "cookies": [c for jar in origin_jars.values() for c in jar.values()]}
    if sessions:
        out["sessions"] = sessions
    if latest_overall:
        out["captured_at"] = latest_overall
    return out


_TRACKER_HINTS = ("google-analytics", "googletagmanager", "doubleclick", "googlesyndication",
    "connect.facebook", "facebook.net", "segment.io", "segment.com", "sentry.io", "sentry-cdn",
    "datadoghq", "browser-intake", "hotjar", "mixpanel", "amplitude", "fullstory", "clarity.ms",
    "tiktok", "bytedance", "volces.com", "volccdn", "cloudflareinsights", "newrelic", "nr-data",
    "optimizely", "appsflyer", "branch.io", "snowplow", "heap.io", "launchdarkly", "intercom",
    "braze", "doubleverify", "scorecardresearch", "quantserve")


def _looks_json(response):
    if "json" in (response.get("mimeType") or "").lower():
        return True
    body = response.get("body")
    if not body:
        return False
    try:
        return isinstance(json.loads(body), (dict, list))
    except (ValueError, TypeError):
        return False


def summarize_capture(capture_path):
    """Offline overview of a capture/HAR: per-origin operation counts, read/write split, whether a
    login is present, and a ready-to-paste discover command. Lets a user pick --origin from a HAR
    without widening scope automatically — the human still chooses which origin(s) to integrate.
    Likely third-party/analytics hosts are flagged and kept out of the suggestion."""
    capture = normalize_capture(load_json(capture_path))
    per = {}
    for ex in capture.get("exchanges", []):
        req = ex.get("request", {})
        try:
            parts = urlsplit(req.get("url", ""))
        except ValueError:
            continue
        if parts.scheme not in ("http", "https") or not parts.hostname:
            continue
        origin = f"{parts.scheme}://{parts.netloc}"
        info = per.setdefault(origin, {"origin": origin, "requests": 0, "read_ops": set(),
            "write_ops": set(), "json_ops": set(), "has_login": False,
            "likely_third_party": any(h in parts.hostname for h in _TRACKER_HINTS)})
        info["requests"] += 1
        method = (req.get("method") or "GET").upper()
        key = (method, parts.path)
        (info["read_ops"] if method in ("GET", "HEAD") else info["write_ops"]).add(key)
        if _looks_json(ex.get("response", {})):
            info["json_ops"].add(key)
        headers = {k.lower(): v for k, v in (req.get("headers") or {}).items()}
        if headers.get("cookie") or headers.get("authorization"):
            info["has_login"] = True
    for origin in capture.get("sessions", {}):
        if origin in per:
            per[origin]["has_login"] = True
    rows = [{"origin": i["origin"], "requests": i["requests"], "read_ops": len(i["read_ops"]),
             "write_ops": len(i["write_ops"]), "json_ops": len(i["json_ops"]),
             "has_login": i["has_login"], "likely_third_party": i["likely_third_party"]}
            for i in per.values()]
    rows.sort(key=lambda r: (r["likely_third_party"], -r["json_ops"], -r["requests"]))
    suggested = [r["origin"] for r in rows if not r["likely_third_party"] and r["json_ops"] > 0][:3]
    command = None
    if suggested:
        origins = " ".join(f"--origin {o}" for o in suggested)
        command = f"python -m web2mcp discover {capture_path} --out <profile> {origins} --read-only-safe"
    return {"origins": rows, "suggested_origins": suggested, "suggested_command": command,
            "note": "Review origins before integrating; third-party/analytics hosts are flagged and excluded from the suggestion."}


def operation_groups(exchanges, policy):
    """Split RPC/GraphQL envelopes by explicitly reviewed JSON discriminators."""
    from .expressions import pointer
    from .client import CallError
    for method, origin, path, samples, parameters in group_exchanges(exchanges):
        setting = policy.get(f'{method} {origin}{path}', policy.get(f'{method} {path}', {}))
        if 'variants' not in setting:
            yield method, origin, path, samples, parameters, setting
            continue
        variants = setting['variants']
        if not isinstance(variants, list) or not 1 <= len(variants) <= 30:
            raise ValueError('Expected 1..30 reviewed RPC variants')
        matched = set()
        for variant in variants:
            match = variant.get('match')
            if not isinstance(match, dict) or not match or not variant.get('name'):
                raise ValueError('RPC variants need name and JSON body match')
            selected = []
            for index, sample in enumerate(samples):
                try: matches = all(pointer(request_parts(sample['request'])[1], p) == v for p, v in match.items())
                except CallError: matches = False
                if matches:
                    if index in matched: raise ValueError('RPC variant matches overlap')
                    matched.add(index); selected.append(sample)
            if not selected: raise ValueError('RPC variant has no observed evidence')
            yield method, origin, path, selected, parameters, {**setting, **variant}


def discover(capture_path, destination, origins, policy=None, *, read_only_safe=False):
    capture = normalize_capture(load_json(capture_path))
    policy = policy or {}
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    tokens = {}
    usable = []
    for ex in capture["exchanges"]:
        req = ex["request"]
        parsed = urlsplit(req["url"])
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in origins or not 200 <= ex["response"]["status"] < 300:
            continue
        if parsed.username or parsed.password:
            continue
        if re.search(r"/(tokens?|sessions?|login|logout|telemetry)(/|$)", parsed.path, re.I):
            continue
        # Non-JSON and streaming interfaces are intentionally not auto-promoted.
        try:
            response_payload(ex)
            request_parts(req)
        except (ValueError, TypeError):
            continue
        usable.append(ex)
    operations = []
    credentials = {"auth": {}, "headers": {}, "cookies": []}
    used_urls = []
    fallback_cookies = []
    for method, origin, path, examples, parameters, setting in operation_groups(usable, policy):
        ex = examples[-1]
        req = ex["request"]
        identity = f"{method} {origin}{path}" + (json.dumps(setting['match'], sort_keys=True) if 'match' in setting else '')
        digest = hashlib.sha256(identity.encode()).hexdigest()[:8]
        if setting.get("exclude"):
            continue
        empty_response = any(sample['response'].get('body') == '' for sample in examples)
        if empty_response and not (setting.get('allow_empty_response') is True and setting.get('read_only') is False):
            continue
        default_name = (method.lower() + "_" + re.sub(r"[^a-zA-Z0-9]+", "_", path).strip("_")[-70:] + "_" + digest)
        name = setting.get("name", default_name)
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,99}", name):
            raise ValueError("Tool names must also be valid Python identifiers")
        auth, headers = captured_auth(req, tokens)
        raw_cookie = headers.pop('cookie', '')
        if raw_cookie and not capture.get('cookies'):
            from http.cookies import SimpleCookie
            parsed_cookies = SimpleCookie(); parsed_cookies.load(raw_cookie)
            for cookie in parsed_cookies.values():
                fallback_cookies.append({'name': cookie.key, 'value': cookie.value, 'domain': urlsplit(req['url']).hostname,
                    'path': urlsplit(req['url']).path.rsplit('/', 1)[0] or '/', 'secure': origin.startswith('https://')})
        authentication = setting.get('authentication', {})
        auth = (storage_auth(req, capture, headers, authentication) if authentication.get('type') == 'storage_session'
                else session_auth(req, capture, headers, auth, authentication))
        credentials["auth"][digest] = auth
        # Headers stay in the PRIVATE credential file, including custom auth headers.
        credentials["headers"][digest] = {k: v for k, v in headers.items()
            if k not in ("host", "content-length", "accept-encoding", "connection") and not k.startswith(":")}
        parts = [request_parts(sample['request']) for sample in examples]
        query, body, body_type = parts[-1]
        if any(re.search(r"token|password|secret|uckey", k, re.I) for part in parts for k in part[0]):
            continue  # Do not bake a URL credential into generated schemas.
        if len({part[2] for part in parts}) > 1:
            continue  # One tool cannot silently switch body encodings.
        if any(sensitive_body(part[1]) for part in parts):
            continue  # Authentication material belongs in private credential adapters.
        prop = {}
        query_keys = list(dict.fromkeys(key for part in parts for key in part[0]))
        if query_keys:
            prop["query"] = {"type": "object", "properties": {k: {"anyOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"}, {"type": "array", "items": {"type": "string"}}]} for k in query_keys}, "additionalProperties": False,
                             'x-observation': infer_schema([part[0] for part in parts]).get('x-observation', {})}
        if body is not None:
            prop["body"] = infer_schema([part[1] for part in parts])
        if parameters:
            prop['path'] = {'type': 'object', 'properties': {name: {'type': 'string', 'minLength': 1} for name in parameters}, 'required': list(parameters), 'additionalProperties': False}
        op = {"name": name, "method": method, "url": origin + path, "query": query,
              "body": body, "body_type": body_type, "credential_ref": digest,
              "read_only": setting.get("read_only", method in ("GET", "HEAD") and not re.search(r"delete|remove|approve|logout|finish", path, re.I)),
              "description": setting.get("description", f"Observed {method} {path}; {len(examples)} sample(s). Server constraints are not fully inferred."),
              "input_schema": {"type": "object", "properties": prop, "additionalProperties": False},
              "timestamp_query": [k for k in query if k in ("_r", "_t")],
              "response_schema": infer_schema([response_payload(sample) for sample in examples]),
              'response_validation': setting.get('response_validation', 'observe'),
              'path_inference': parameters,
              'evidence': [{'sample_index': i, 'action': sample.get('label', ''), 'response_fingerprint': fingerprint(response_payload(sample))} for i, sample in enumerate(examples)],
              "samples": len(examples), "verification": "captured_only"}
        if parameters: op['input_schema']['required'] = ['path']
        if setting.get('input_schema'): op['input_schema'] = setting['input_schema']
        if setting.get('csrf'): op['csrf'] = setting['csrf']
        if setting.get('session_validation'): op['session_validation'] = setting['session_validation']
        if setting.get('timestamp_query'): op['timestamp_query'] = setting['timestamp_query']
        if setting.get('match'): op['request_body_matches'] = setting['match']
        if empty_response: op['allow_empty_response'] = True
        used_urls.extend(sample['request']['url'] for sample in examples)
        operations.append(op)
    names = [o["name"] for o in operations]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate tool name in policy")
    refs = {op['credential_ref'] for op in operations}
    credentials['auth'] = {k:v for k,v in credentials['auth'].items() if k in refs}
    credentials['headers'] = {k:v for k,v in credentials['headers'].items() if k in refs}
    credentials['cookies'] = scoped_cookies(capture.get('cookies', []) + fallback_cookies, used_urls)
    manifest = {"version": 2, "allowed_origins": sorted(origins), "operations": operations,
                "limitations": ["Observed flows only; no completeness guarantee", "Observed presence/constant values are candidates, not required/enum rules", "Identifier-shaped path inference requires trial validation", "Writes preview unless confirmed", "Static signatures require adapters"]}
    if read_only_safe:
        # One-step safe read-only profile: hide every non-read operation and refuse any
        # non-GET/HEAD call at the execution layer. Writes stay in the manifest for review
        # but are neither exposed as MCP tools nor callable until deliberately re-enabled.
        for op in operations:
            if not op.get("read_only"):
                op["internal"] = True
        manifest["read_only_mode"] = True
        manifest["allowed_methods"] = ["GET", "HEAD"]
    save_private(root / "manifest.json", manifest)
    save_private(root / "credentials.json", credentials)
    paths = {}
    for op in operations:
        parsed = urlsplit(op["url"])
        # Prefixing origin in a separate server entry supports multiple backends.
        paths.setdefault(parsed.path, {})[op["method"].lower()] = {
            "operationId": op["name"], "servers": [{"url": f"{parsed.scheme}://{parsed.netloc}"}],
            "description": op["description"], "x-web2mcp-read-only": op["read_only"],
            "parameters": [{"name": k, "in": "query", "schema": {"type": "string"}, "required": False} for k in op['input_schema']['properties'].get('query', {}).get('properties', {})] +
                [{'name':k, 'in':'path', 'schema':{'type':'string'}, 'required':True} for k in op['path_inference']],
            "responses": {"200": {"description": "Observed response shape", "content": {"application/json": {"schema": op["response_schema"]}}}}}
        if op["body"] is not None:
            paths[parsed.path][op["method"].lower()]["requestBody"] = {"content": {"application/json" if op["body_type"] == "json" else "application/x-www-form-urlencoded": {"schema": op['input_schema']['properties']['body']}}}
        if op.get('allow_empty_response') and op['response_schema'].get('type') == 'string':
            paths[parsed.path][op['method'].lower()]['responses']['200'] = {'description': 'Observed empty response; business completion requires contract read-back verification.'}
    grouped = defaultdict(list)
    for op in operations: grouped[(urlsplit(op['url']).path, op['method'].lower())].append(op)
    for (path, method), variants in grouped.items():
        if len(variants) < 2: continue
        entry = paths[path][method]
        entry['description'] = 'Multiple observed tools share this HTTP endpoint; select by reviewed request-body discriminator.'
        entry['x-web2mcp-read-only'] = all(op['read_only'] for op in variants)
        entry['x-web2mcp-variants'] = [{'operationId': op['name'], 'read_only':op['read_only'],
            'match':op.get('request_body_matches', {}), 'input_schema':op['input_schema']} for op in variants]
        entry['responses']['200']['content']['application/json']['schema'] = {'anyOf':[op['response_schema'] for op in variants]}
        if all(op['body_type'] == 'json' for op in variants):
            entry['requestBody'] = {'content': {'application/json': {'schema': {'anyOf':[
                op['input_schema']['properties']['body'] for op in variants]}}}}
    save_private(root / "openapi.json", {"openapi": "3.1.0", "info": {"title": "Browser-observed API", "version": "0.1.0"}, "paths": paths})
    server = 'from pathlib import Path\nfrom web2mcp.server import serve\nif __name__ == "__main__":\n    serve(Path(__file__).resolve().parent)\n'
    (root / "server.py").write_text(server)
    code = 'from pathlib import Path\nfrom web2mcp import Client\n\nclass GeneratedClient(Client):\n    def __init__(self):\n        super().__init__(Path(__file__).resolve().parent)\n'
    for op in operations:
        code += f'\n    def {op["name"]}(self, *, path=None, query=None, body=None, confirm=False):\n        arguments = {{}}\n        if path is not None: arguments["path"] = path\n        if query is not None: arguments["query"] = query\n        if body is not None: arguments["body"] = body\n        return self.call({op["name"]!r}, arguments, confirm=confirm)\n'
    (root / "client.py").write_text(code)
    save_private(root / "mcp.json", {"mcpServers": {"web2mcp": {"command": sys.executable, "args": [str(root / "server.py")]}}})
    return {"profile": str(root), "operations": len(operations), "read_only": sum(o["read_only"] for o in operations),
            "auth_adapters": sorted(set(v["type"] for v in credentials["auth"].values()))}
