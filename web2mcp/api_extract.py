"""Static API-map extraction from a web app's shipped JavaScript.

Downloads the JS the app actually loads (entry bundle + lazy route chunks) and regex-extracts
the API surface — endpoint paths, HTTP methods, read/write classification — so the connected AI
can review the full surface instead of discovering it by capture alone. Minified code means this
is a best-effort map (dynamic paths, custom HTTP wrappers), not a guaranteed-complete spec; it is
untrusted product data, never executed. Pairs with explorer_survey (active runtime capture).
"""
import json
import re
import time
from urllib.parse import urlsplit

# HTTP-client method calls: `client.post("/path"` / `client.get(`/path${...}` — the most common form.
_METHOD_CALL = re.compile(r'(\w+)\.(get|post|put|patch|delete)\s*\(\s*[`"\']([^`"\'\s${]+)', re.I)
# fetch("/path", {... method: "POST" ...})
_FETCH = re.compile(r'fetch\s*\(\s*[`"\']([^`"\'\s${]+)[`"\']\s*,\s*\{([^}]{0,120})', re.I)
# Standalone API-looking path literals (REST paths with a version or /api/ segment).
_PATH_LITERAL = re.compile(r'[`"\'](\/(?:[\w.-]+\/)*(?:v\d[\w.]*|api|rest|graphql)(?:\/[\w.-]+)+)[`"\']', re.I)
_METHOD_FIELD = re.compile(r'method\s*:\s*[`"\'](get|post|put|patch|delete)[`"\']', re.I)
# Custom HTTP wrappers often take an endpoint NAME, not a full path, e.g. Ot("job_create").
# These verb-like names (…_create/_list/_action/…) are lower-confidence candidates: the full
# path and method live in the wrapper, so we surface the names for the AI to resolve.
_WRAP_NAME = re.compile(r'[`"\']([a-z][a-z0-9]*(?:_[a-z0-9]+)*_(?:create|list|action|delete|update|detail|save|submit|send|feed|cancel|start|stop|interrupt|get|query|search|export|import|exec))[`"\']')
_VERB = re.compile(r'_(create|delete|update|save|submit|send|cancel|start|stop|interrupt|action|exec|import)$')

_READ = {"GET", "HEAD"}


def _kind(method):
    if method in _READ:
        return "read"
    if method == "?":
        return "unknown"
    return "write"


def extract(sources):
    """sources: list of {"url": ..., "text": ...}. Returns {generated, sources, endpoints}."""
    found = {}  # (method, path) -> record

    def add(method, path, client, snippet):
        path = path.split("?", 1)[0].rstrip("/")
        if not path or len(path) > 200:
            return
        method = (method or "?").upper()
        key = (method, path)
        rec = found.get(key)
        if not rec:
            found[key] = {"method": method, "path": path, "kind": _kind(method),
                          "clients": set([client] if client else []), "sample": snippet[:160]}
        elif client:
            found[key]["clients"].add(client)

    for src in sources:
        text = src.get("text") or ""
        for m in _METHOD_CALL.finditer(text):
            client, method, path = m.group(1), m.group(2).upper(), m.group(3)
            add(method, path, client, text[max(0, m.start()):m.start() + 160])
        for m in _FETCH.finditer(text):
            path, opts = m.group(1), m.group(2)
            mm = _METHOD_FIELD.search(opts)
            add((mm.group(1).upper() if mm else "GET"), path, "fetch", text[max(0, m.start()):m.start() + 160])
        # Standalone path literals whose method we could not bind — record as unknown-method candidates.
        for m in _PATH_LITERAL.finditer(text):
            path = m.group(1)
            if not any(p == path.split("?", 1)[0].rstrip("/") for (_, p) in found):
                add("?", path, "", text[max(0, m.start()):m.start() + 160])

    endpoints = []
    for rec in found.values():
        rec["clients"] = sorted(rec["clients"])
        endpoints.append(rec)
    # Writes first (they need authorization), then reads, then unknowns; alphabetical within.
    order = {"write": 0, "unknown": 1, "read": 2}
    endpoints.sort(key=lambda r: (order[r["kind"]], r["path"], r["method"]))
    # Lower-confidence wrapper endpoint names (full path/method not inferable from a literal).
    names = {}
    for src in sources:
        for m in _WRAP_NAME.finditer(src.get("text") or ""):
            n = m.group(1)
            if n not in names:
                names[n] = {"name": n, "likely_write": bool(_VERB.search(n))}
    candidates = sorted(names.values(), key=lambda r: (not r["likely_write"], r["name"]))
    return {"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "sources": [s.get("url") for s in sources],
            "endpoints": endpoints, "name_candidates": candidates}


def summarize(result):
    """Human-readable one-liner per endpoint, grouped by kind."""
    by = {"write": [], "unknown": [], "read": []}
    for e in result["endpoints"]:
        by[e["kind"]].append(f"  {e['method']:6} {e['path']}" + (f"  [{','.join(e['clients'])}]" if e["clients"] else ""))
    out = [f"# API map from {len(result['sources'])} JS source(s), {len(result['endpoints'])} endpoints"]
    for kind, label in (("write", "写(需授权)"), ("read", "读"), ("unknown", "方法未定(候选)")):
        if by[kind]:
            out.append(f"\n## {label} ({len(by[kind])})")
            out.extend(by[kind])
    cands = result.get("name_candidates") or []
    if cands:
        out.append(f"\n## 封装端点名候选 ({len(cands)}，完整路径/方法需查 HTTP 封装函数)")
        out.extend(f"  {c['name']}" + ("  (疑似写)" if c["likely_write"] else "") for c in cands)
    return "\n".join(out)
