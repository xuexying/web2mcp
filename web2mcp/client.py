"""Browser-free HTTP execution, with per-operation scope and no automatic retries."""
import copy
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone, timedelta
from http.cookiejar import Cookie
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit, urlencode, quote, unquote

import httpx
import jsonschema

from .storage import load_json, save_private
from .audit import Audit, redact
from .evidence import schema_changes, fingerprint
from .sessions import request_session_headers
from .storage_sessions import storage_session_headers


class CallError(RuntimeError):
    def __init__(self, message, *, status=None, private_response=None, reason='invalid_request', outcome='not_sent', call_id=None):
        super().__init__(message)
        self.status = status
        self.private_response = private_response
        self.reason = reason
        self.outcome = outcome
        self.call_id = call_id

    def as_dict(self):
        return {'error': str(self), 'reason': self.reason, 'outcome': self.outcome,
                'http_status': self.status, 'call_id': self.call_id, 'automatic_retry': False}


class Client:
    def __init__(self, profile, transport=None, credentials=None):
        self.root = Path(profile).resolve()
        self.manifest = load_json(self.root / "manifest.json")
        self.credentials = credentials if credentials is not None else load_json(self.root / "credentials.json")
        self.audit = Audit(self.root, sensitive=self.manifest.get('sensitive_responses', False))
        self.operations = {op["name"]: op for op in self.manifest["operations"]}
        self.http = httpx.Client(timeout=30, follow_redirects=False, transport=transport)
        for c in self.credentials.get("cookies", []):
            expires = c.get("expires", -1)
            self.http.cookies.jar.set_cookie(Cookie(
                version=0, name=c["name"], value=c["value"], port=None,
                port_specified=False, domain=c["domain"], domain_specified=c["domain"].startswith('.'),
                domain_initial_dot=c["domain"].startswith('.'), path=c.get("path", "/"),
                path_specified=True, secure=c.get("secure", False),
                expires=int(expires) if expires and expires > 0 else None,
                discard=not expires or expires < 0, comment=None, comment_url=None,
                rest={}, rfc2109=False))

    def close(self):
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def describe(self):
        return [{"name": o["name"], "method": o["method"], "url": o["url"],
                 "read_only": o.get("read_only", False), "description": o["description"],
                 "inputSchema": o["input_schema"]} for o in self.operations.values()]

    def _request(self, name, arguments):
        if name not in self.operations:
            raise CallError("Unknown operation")
        op = self.operations[name]
        if self.manifest.get('read_only_mode') and (op.get('read_only') is not True or
                op['method'] not in self.manifest.get('allowed_methods', ['GET', 'HEAD'])):
            raise CallError('Profile permits only reviewed read operations', reason='read_only_scope')
        try:
            jsonschema.validate(arguments, op["input_schema"])
        except jsonschema.ValidationError as e:
            raise CallError(f"Invalid arguments at {'/'.join(map(str,e.path))}: {e.validator}") from None
        url = op["url"]
        for key, val in arguments.get("path", {}).items():
            if "/" in str(val) or str(val) in (".", ".."):
                raise CallError("Path parameter must be one non-relative segment")
            url = url.replace("{" + key + "}", quote(str(val), safe=""))
        if "{" in url:
            raise CallError("Missing path parameter")
        origin = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"
        if origin not in self.manifest["allowed_origins"]:
            raise CallError("Origin is outside the recorded profile")
        if urlsplit(url).username or urlsplit(url).password:
            raise CallError('URL credentials are not supported')
        query = copy.deepcopy(op.get("query", {}))
        query.update(arguments.get("query", {}))
        for key in op.get("timestamp_query", []):
            query[key] = str(int(time.time() * 1000))
        body = copy.deepcopy(op.get("body"))
        if "body" in arguments:
            body = arguments["body"]
        if op.get('body_clock_fields') or op.get('headers_from_body'):
            raise CallError('Dynamic request transforms require a reviewed custom adapter')
        if op.get('request_body_matches'):
            from .expressions import pointer
            if any(pointer(body, path) != value for path, value in op['request_body_matches'].items()):
                raise CallError('Request changes a reviewed RPC discriminator')
        headers = dict(self.credentials.get("headers", {}).get(op["credential_ref"], {}))
        headers.pop("authorization", None)
        auth = self.credentials.get("auth", {}).get(op["credential_ref"], {"type": "none"})
        if auth.get('type') not in ('none', 'static', 'unsupported', 'cookie_session', 'storage_session'):
            raise CallError('Authentication requires a reviewed custom adapter')
        if auth["type"] == "static":
            headers["authorization"] = auth["value"]
        elif auth["type"] == "unsupported":
            raise CallError("Authentication requires an adapter; static signature replay is disabled")
        elif auth['type'] == 'cookie_session':
            # Discard legacy headers/jar selections for this strategy. Only the
            # exact-origin snapshot contributes Cookie and authentication headers.
            headers, reason = request_session_headers(auth, url)
            if reason:
                self.require_session_relay(name, reason)
        elif auth['type'] == 'storage_session':
            headers, reason = storage_session_headers(auth, url)
            if reason: self.require_session_relay(name, reason)
        data_args = {}
        if body is not None:
            if op.get("body_type") == "form":
                data_args["data"] = body
            else:
                data_args["json"] = body
        request = self.http.build_request(op["method"], url, params=list_pairs(query), headers=headers, **data_args)
        if auth['type'] == 'storage_session':
            # A storage-token session never inherits cookies from an unrelated strategy.
            request.headers.pop('cookie', None)
        if op.get('csrf'):
            # Bind only the cookie that the HTTP jar would send to this exact
            # origin/path. Never copy a token from an unrelated domain.
            csrf = op['csrf']
            header = csrf.get('header', '').lower()
            if header not in ('x-csrf-token', 'x-xsrf-token', 'csrf-token', 'x-csrftoken') or not isinstance(csrf.get('cookie'), str):
                raise CallError('Unsupported CSRF cookie/header configuration')
            cookies = SimpleCookie()
            cookies.load(request.headers.get('cookie', ''))
            if csrf['cookie'] not in cookies: raise CallError('CSRF cookie missing; run the session preflight', reason='csrf_required')
            value = cookies[csrf['cookie']].value
            request.headers[header] = unquote(value) if csrf.get('url_decode', False) else value

        return op, request

    def require_session_relay(self, name, cause, status=None, *, outcome='not_sent'):
        save_private(self.root/'cards'/'login.json', {
            'type': 'login', 'state': 'session_relay_required', 'reason': 'session_relay_required',
            'cause': cause, 'operation': name, 'http_status': status, 'automatic_replay': False,
            'action': '用户在浏览器确认登录后重新采集会话，再执行 refresh-login；不自动重登或重放。'})
        raise CallError('SESSION_RELAY_REQUIRED: 需要重新做一次会话接力；不自动登录或重放。',
                        reason='session_relay_required', status=status, outcome=outcome)

    def call(self, name, arguments=None, *, confirm=False):
        arguments = arguments or {}
        call_id = uuid.uuid4().hex
        started = time.monotonic()
        logged_name = name if name in self.operations else '<unknown>'
        self.audit.append(call_id=call_id, event='start', tool=logged_name,
                          arguments_hmac=self.audit.arguments_hash(arguments))
        state = {'sent': False, 'write': not self.operations.get(name, {}).get('read_only', True)}
        try:
            result = self._execute(name, arguments, confirm=confirm, call_id=call_id, state=state)
            self.audit.append(call_id=call_id, event='finish', tool=logged_name,
                              outcome='preview' if result.get('preview') else 'response_received',
                              http_status=result.get('status'), elapsed_ms=round((time.monotonic()-started)*1000))
            return result
        except CallError as error:
            error.call_id = call_id
            if state['sent'] and error.outcome == 'not_sent':
                error.outcome = 'unknown' if state['write'] else 'read_failed'
            self.audit.append(call_id=call_id, event='error', tool=logged_name, reason=error.reason,
                              outcome=error.outcome, http_status=error.status,
                              elapsed_ms=round((time.monotonic()-started)*1000))
            raise
        except Exception:
            outcome = ('unknown' if state['write'] else 'read_failed') if state['sent'] else 'not_sent'
            self.audit.append(call_id=call_id, event='error', tool=logged_name, reason='internal', outcome=outcome)
            raise CallError('Internal execution failure; inspect private state before reuse',
                            reason='internal', outcome=outcome, call_id=call_id) from None

    def _execute(self, name, arguments, *, confirm, call_id, state):
        op, request = self._request(name, arguments)
        if not op.get("read_only", False) and confirm is not True:
            return {"preview": True, "operation": name, "method": op["method"],
                    "url": str(request.url), "arguments": arguments,
                    "body": request.content.decode("utf-8") if request.content else None,
                    "message": "Write not sent. Execute with confirm=True after choosing the business action."}
        # A request gets a fresh MAC nonce on every invocation. No automatic
        # retries: a timed-out write can have succeeded at the server.
        self.audit.append(call_id=call_id, event='dispatch', tool=name,
                          credential_ref=op['credential_ref'], method=op['method'])
        state['sent'] = True
        try:
            response = self.http.send(request)
        except httpx.HTTPError:
            raise CallError("Network failure; inspect server state before retrying a write.", reason='network',
                            outcome='unknown' if state['write'] else 'read_failed') from None
        auth = self.credentials.get('auth', {}).get(op['credential_ref'], {})
        cookie_session = auth.get('type') in ('cookie_session', 'storage_session')
        if cookie_session and response.status_code in (401, 403):
            # A 403 may also be a permission/risk-control refusal; do not claim
            # that expired cookies are the proven cause or retry after a refresh.
            self.require_session_relay(name, 'authentication_or_access_rejected', response.status_code, outcome='rejected')
        if response.status_code == 401:
            save_private(self.root/'cards'/'login.json', {'type':'login', 'reason':'login',
                'state':'login_required', 'automatic_replay':False, 'operation':name})
            raise CallError('AUTH_REQUIRED (401): refresh the current user login', status=401, reason='login', outcome='rejected')
        if response.status_code == 403:
            raise CallError('PERMISSION_DENIED (403): current identity lacks permission', status=403, reason='permission', outcome='rejected')
        if 300 <= response.status_code < 400:
            if cookie_session:
                self.require_session_relay(name, 'redirect_received', response.status_code, outcome='unknown' if state['write'] else 'read_failed')
            raise CallError("Redirect blocked; inspect authentication/endpoint changes", status=response.status_code, reason='redirect')
        if response.status_code >= 400:
            try:
                error_body = response.json()
            except ValueError:
                error_body = response.text[:16000]
            diagnostic_id = uuid.uuid4().hex
            save_private(self.root / "diagnostics" / (diagnostic_id + ".json"), {
                "operation": name, "status": response.status_code, "response":
                '[omitted: sensitive response]' if self.manifest.get('sensitive_responses') else redact(error_body)})
            raise CallError(f"Upstream HTTP {response.status_code}; private diagnostic {diagnostic_id}",
                            status=response.status_code, private_response=None if self.manifest.get('sensitive_responses') else error_body, reason='upstream',
                            outcome='unknown' if state['write'] else 'read_failed')
        try:
            payload = response.json()
        except ValueError:
            if "text/html" in response.headers.get("content-type", ""):
                if cookie_session:
                    self.require_session_relay(name, 'unexpected_html', response.status_code, outcome='unknown' if state['write'] else 'read_failed')
                raise CallError("Expected API response, received HTML (possible login page)", reason='unexpected_html') from None
            payload = response.text
        result = {"status": response.status_code, "data": payload}
        if op.get('session_validation'):
            # Configured from observed payload semantics, not a site-specific
            # heuristic. An anonymous HTTP 200 must not be reported as success.
            from .expressions import pointer
            for check in op['session_validation']:
                try: actual = pointer(payload, check['pointer'])
                except CallError: actual = object()
                if type(actual) is not type(check['expected']) or actual != check['expected']:
                    if check.get('kind', 'authenticated') == 'identity':
                        raise CallError('Session identity changed; review the signed-in account', reason='identity_mismatch', outcome='read_failed')
                    self.require_session_relay(name, 'session_payload_rejected', response.status_code, outcome='unknown' if state['write'] else 'read_failed')
        if op.get('response_schema') and op.get('response_validation', 'observe') != 'off':
            errors, warnings = schema_changes(op['response_schema'], payload)
            if errors or warnings:
                changes = {'errors': errors, 'warnings': warnings}
                card_id = fingerprint({'operation': name, 'changes': changes})[:24]
                save_private(self.root/'cards'/(card_id+'.json'), {'type': 'tool_update', 'operation': name,
                             'changes': changes, 'state': 'review_required', 'automatic_replay': False})
                result['schema_observation'] = {**changes, 'card_id': card_id}
                if errors and op.get('response_validation') == 'strict':
                    raise CallError('Response structure changed; review tool_update before reuse',
                                    reason='schema_changed', outcome='unknown' if state['write'] else 'read_failed')
        return result


def list_pairs(query):
    pairs = []
    for key, value in query.items():
        for val in value if isinstance(value, list) else [value]:
            if val is not None:
                pairs.append((key, str(val).lower() if isinstance(val, bool) else str(val)))
    return pairs
