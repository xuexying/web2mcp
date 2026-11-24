"""`web2mcp add-curl`: turn one "Copy as cURL" command into a profile operation.

The lowest-friction way in — for someone who just wants to wire up a single endpoint.
A cURL command is parsed into a method/URL/headers/body, recorded as one synthetic
exchange in a per-profile source capture, and the whole accumulated capture is run back
through the normal `discover` pipeline. So read/write classification, credential handling
and the safety rules are exactly the same as for a real capture; repeated `add-curl`
calls accumulate into one profile. Nothing is sent.

Use the bash form of "Copy as cURL" (Chrome/Firefox DevTools → right-click a request).
"""

import base64
import re
import shlex
from pathlib import Path
from urllib.parse import urlsplit

from .client import CallError
from .discover import discover
from .storage import load_json, save_private

_VALUE_OPTS = {
    "-X",
    "--request",
    "-H",
    "--header",
    "-b",
    "--cookie",
    "-u",
    "--user",
    "--url",
    "-e",
    "--referer",
    "-A",
    "--user-agent",
}
_DATA_OPTS = {"-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--data-urlencode"}


def parse_curl(text):
    """Parse a bash-form cURL command into (method, url, headers, body). Raises CallError
    on anything it cannot turn into a single HTTP request."""
    text = re.sub(r"\\\r?\n", " ", text.strip())
    if not text:
        raise CallError("Empty cURL command")
    try:
        tokens = shlex.split(text)
    except ValueError as error:
        raise CallError(f"Could not parse the cURL command: {error}") from None
    if tokens and tokens[0] == "curl":
        tokens = tokens[1:]
    method = None
    url = None
    headers = {}
    data = None
    force_get = False
    i = 0
    while i < len(tokens):
        t = tokens[i]
        takes_value = t in _VALUE_OPTS or t in _DATA_OPTS or t == "--json"
        if takes_value and i + 1 >= len(tokens):
            raise CallError(f"cURL option {t} is missing its value")
        if t in ("-X", "--request"):
            method = tokens[i + 1]
            i += 2
        elif t in ("-H", "--header"):
            name, sep, value = tokens[i + 1].partition(":")
            if sep:
                headers[name.strip()] = value.strip()
            i += 2
        elif t in _DATA_OPTS:
            piece = tokens[i + 1]
            data = piece if data is None else data + "&" + piece
            i += 2
        elif t == "--json":
            data = tokens[i + 1]
            headers.setdefault("Content-Type", "application/json")
            headers.setdefault("Accept", "application/json")
            i += 2
        elif t in ("-b", "--cookie"):
            headers["Cookie"] = tokens[i + 1]
            i += 2
        elif t in ("-u", "--user"):
            headers["Authorization"] = "Basic " + base64.b64encode(tokens[i + 1].encode()).decode()
            i += 2
        elif t in ("-e", "--referer"):
            headers["Referer"] = tokens[i + 1]
            i += 2
        elif t in ("-A", "--user-agent"):
            headers["User-Agent"] = tokens[i + 1]
            i += 2
        elif t == "--url":
            url = tokens[i + 1]
            i += 2
        elif t in ("-G", "--get"):
            force_get = True
            i += 1
        elif t.startswith("-"):
            i += 1  # valueless or ignored flag (-L, --compressed, -k, -s, -i, …)
        else:
            if url is None:
                url = t
            i += 1
    if not url:
        raise CallError("No URL found in the cURL command")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise CallError("cURL URL must be absolute http(s)")
    if method is None:
        method = "GET" if (data is None or force_get) else "POST"
    method = method.upper()
    if force_get and data:
        url = url + ("&" if "?" in url else "?") + data
        data = None
    if data is not None and not any(k.lower() == "content-type" for k in headers):
        headers["Content-Type"] = (
            "application/json" if data.lstrip()[:1] in "{[" else "application/x-www-form-urlencoded"
        )
    return method, url, headers, data


def add_curl(profile, curl_text, *, origins=None, read_only_safe=False):
    """Add the operation described by one cURL command to ``profile`` (created if absent).
    Returns a summary; nothing is sent. Repeated calls accumulate into the same profile."""
    method, url, headers, data = parse_curl(curl_text)
    root = Path(profile)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    src = root / "curl-sources.json"
    capture = (
        load_json(src) if src.exists() else {"format": "web2mcp-capture-v1", "exchanges": [], "cookies": []}
    )
    capture["exchanges"].append(
        {
            "request": {"method": method, "url": url, "headers": headers, "postData": data},
            # cURL carries no response; a placeholder lets discover build the op. The real
            # response shape is filled in later by a trial-read, not guessed here.
            "response": {"status": 200, "mimeType": "application/json", "body": "{}"},
            "label": "Imported from cURL",
        }
    )
    save_private(src, capture)
    if origins is None:
        origins = sorted(
            {
                f"{urlsplit(e['request']['url']).scheme}://{urlsplit(e['request']['url']).netloc}"
                for e in capture["exchanges"]
            }
        )
    result = discover(src, root, origins, read_only_safe=read_only_safe)
    return {
        "profile": result["profile"],
        "added": f"{method} {urlsplit(url).path}",
        "operations": result["operations"],
        "read_only": result["read_only"],
        "origins": origins,
        "next": "trial-read a read op to capture its real response shape; writes preview until "
        "you confirm. Run `web2mcp serve " + result["profile"] + "`.",
    }
