"""Origin-bound browser session relay. Values stay in private credentials.json."""
import copy
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

TRANSPORT_HEADERS = {'cookie', 'host', 'content-length', 'accept-encoding', 'connection',
                     'proxy-authorization', 'proxy-connection', 'transfer-encoding'}


def origin_of(url):
    p = urlsplit(url)
    return f'{p.scheme}://{p.netloc}'


def cookie_matches(cookie, url, *, now=None, check_path=True):
    p = urlsplit(url)
    host = (p.hostname or '').lower()
    domain = str(cookie.get('domain', '')).lower()
    bare = domain.lstrip('.')
    host_only = cookie.get('hostOnly', not domain.startswith('.'))
    if not bare or not (host == bare or not host_only and host.endswith('.' + bare)):
        return False
    if cookie.get('secure') and p.scheme != 'https':
        return False
    path, target = cookie.get('path', '/') or '/', p.path or '/'
    if check_path and not (target == path or target.startswith(path if path.endswith('/') else path + '/')):
        return False
    expires = cookie.get('expires', -1)
    if now is not None and expires and expires > 0 and expires <= now:
        return False
    return True


def session_auth(request, capture, headers, previous_auth, policy=None):
    """Infer cookie auth only from observed cookies; reject unknown signatures."""
    policy = policy or {}
    origin = origin_of(request['url'])
    snapshots = capture.get('sessions', {})
    snapshot = snapshots.get(origin, {})
    cookies = [copy.deepcopy(c) for c in snapshot.get('cookies', capture.get('cookies', []))
               if cookie_matches(c, request['url'], check_path=False)]
    requested = policy.get('type') == 'cookie_session'
    if not requested and not snapshot:
        return previous_auth  # Legacy HAR keeps its prior cookie-jar behavior.
    if previous_auth['type'] == 'unsupported':
        if requested:
            raise ValueError('Cookie sessions accept only a temporary bearer header; unknown signature adapters cannot be replaced')
        return previous_auth
    if not cookies and not requested:
        return previous_auth
    if not cookies:
        raise ValueError('Cookie session requires an observed browser cookie snapshot')
    if any(c.get('partitionKey') or c.get('partitionKeyOpaque') for c in cookies):
        raise ValueError('Partitioned browser cookies require a context-aware adapter')
    safe_headers = {k.lower(): v for k, v in headers.items()
                    if k.lower() not in TRANSPORT_HEADERS and not k.startswith(':')}
    if previous_auth['type'] == 'static':
        if not previous_auth['value'].lower().startswith('bearer '):
            raise ValueError('Only temporary bearer authorization can accompany cookie sessions')
        safe_headers['authorization'] = previous_auth['value']
    selected = policy.get('header_names', list(safe_headers))
    if not isinstance(selected, list) or any(k != k.lower() or k not in safe_headers for k in selected):
        raise ValueError('Session header selection must come from the observed request')
    required = policy.get('required_cookie_names', [])
    if not isinstance(required, list) or any(n not in {c['name'] for c in cookies} for n in required):
        raise ValueError('Required session cookies must be observed')
    captured_at = snapshot.get('captured_at') or capture.get('captured_at')
    if not captured_at:
        # Legacy HAR has no authoritative login time; require explicit recapture.
        captured_at = '1970-01-01T00:00:00+00:00'
    age = policy.get('max_age_seconds', 86400)
    if type(age) is not int or not 1 <= age <= 30 * 86400:
        raise ValueError('Session maximum age must be 1 second to 30 days')
    return {'type': 'cookie_session', 'session': {
        'origin': origin, 'captured_at': captured_at, 'max_age_seconds': age,
        'cookie_names': sorted({c['name'] for c in cookies}), 'required_cookie_names': required,
        'header_names': sorted(selected), 'cookies': cookies,
        'headers': {k: safe_headers[k] for k in selected}}}


def request_session_headers(auth, url):
    """Return scoped headers or an expiration reason, without disclosing values."""
    session = auth.get('session', {})
    if session.get('origin') != origin_of(url):
        return None, 'session_origin_mismatch'
    try:
        captured = datetime.fromisoformat(session['captured_at'].replace('Z', '+00:00'))
        if captured.tzinfo is None:
            raise ValueError()
        age = time.time() - captured.timestamp()
        max_age = session['max_age_seconds']
        if type(max_age) is not int or not 1 <= max_age <= 30 * 86400 or age < -300 or age > max_age:
            return None, 'session_capture_expired'
        cookies = [c for c in session['cookies'] if cookie_matches(c, url, now=time.time())]
        if not cookies or not set(session.get('required_cookie_names', [])) <= {c['name'] for c in cookies}:
            return None, 'session_cookie_missing_or_expired'
        if any(c.get('partitionKey') or c.get('partitionKeyOpaque') for c in cookies):
            return None, 'partitioned_session_unsupported'
        for c in cookies:
            if (not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-zA-Z-]+", c['name'])
                    or any(ch in c['value'] for ch in '\r\n;')):
                return None, 'invalid_cookie_session'
        headers = dict(session['headers'])
        if set(headers) != set(session['header_names']) or any(
                k != k.lower() or k in TRANSPORT_HEADERS or not re.fullmatch('[a-z0-9-]+', k)
                or not isinstance(v, str) or '\r' in v or '\n' in v for k, v in headers.items()):
            return None, 'invalid_session_headers'
        # RFC cookie ordering matters when same-name cookies use different paths.
        cookies.sort(key=lambda c: len(c.get('path', '/')), reverse=True)
        headers['cookie'] = '; '.join(c['name'] + '=' + c['value'] for c in cookies)
        return headers, None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None, 'invalid_cookie_session'
