"""Configurable browser-storage source; fresh signing, never captured MAC replay."""
import copy
import hmac
import json
import re
import time
from datetime import datetime

from .sessions import TRANSPORT_HEADERS, origin_of


def _pointer(value, path):
    if path == '': return value
    if not isinstance(path, str) or not path.startswith('/'): raise ValueError('Invalid storage JSON pointer')
    try:
        for part in path[1:].split('/'):
            part = part.replace('~1', '/').replace('~0', '~')
            value = value[int(part)] if isinstance(value, list) else value[part]
        return value
    except (KeyError, IndexError, TypeError, ValueError):
        raise ValueError('Configured storage field is missing') from None


def _epoch(value):
    if not isinstance(value, str): raise ValueError('Token expiry must be an ISO timestamp with timezone')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None: raise ValueError('Token expiry needs timezone')
    return parsed.timestamp()


def storage_auth(request, capture, headers, recipe):
    if recipe.get('type') != 'storage_session': raise ValueError('Expected storage_session recipe')
    sources = [s for s in capture.get('storage_sources', []) if s['id'] == recipe.get('source_id')]
    if len(sources) != 1: raise ValueError('Storage source is missing or ambiguous')
    source = sources[0]
    if (source.get('origin') != recipe.get('source_origin') or source.get('area') not in ('localStorage', 'sessionStorage')
            or not isinstance(source.get('value'), str) or re.search('password|passwd|passphrase', source.get('key', ''), re.I)):
        raise ValueError('Storage source identity or value is invalid')
    value = source['value']
    decode = recipe.get('decode', [])
    if not isinstance(decode, list) or len(decode) > 8: raise ValueError('Storage decoding exceeds limit')
    for action in decode:
        if action == 'json':
            try: value = json.loads(value)
            except (ValueError, TypeError): raise ValueError('Storage JSON decoding failed') from None
        elif isinstance(action, dict) and set(action) == {'pointer'}: value = _pointer(value, action['pointer'])
        else: raise ValueError('Only JSON decoding and pointers are supported')
    identity = None
    if 'identity_pointer' in recipe:
        identity = _pointer(value, recipe['identity_pointer'])
        if type(identity) not in (str, int) or str(identity) != recipe.get('identity_expected'):
            raise ValueError('Storage session identity changed')
    spec = recipe.get('strategy', {})
    if spec.get('type') == 'bearer':
        token = _pointer(value, spec['token_pointer'])
        if not isinstance(token, str) or not token or any(c in token for c in '\r\n'):
            raise ValueError('Invalid temporary bearer token')
        captured = next((v for k,v in request.get('headers', {}).items() if k.lower() == 'authorization'), '')
        if captured and not hmac.compare_digest(captured, 'Bearer '+token): raise ValueError('Storage token does not match the observed bearer header')
        strategy = {'type':'bearer', 'token':token}
    else: raise ValueError('Unsupported storage authentication strategy')
    expires_at = _pointer(value, recipe['expires_at_pointer'])
    _epoch(expires_at); _epoch(source['captured_at'])
    age = recipe.get('max_age_seconds', 86400)
    if type(age) is not int or not 1 <= age <= 30*86400: raise ValueError('Invalid storage capture maximum age')
    available = {k.lower():v for k,v in headers.items() if k.lower() not in TRANSPORT_HEADERS | {'authorization'} and not k.startswith(':')}
    selected = recipe.get('header_names', list(available))
    if not isinstance(selected, list) or any(k != k.lower() or k not in available for k in selected):
        raise ValueError('Session headers must be observed and cannot include credentials or transport headers')
    return {'type':'storage_session', 'session':{
        'origin':origin_of(request['url']), 'source':{k:source[k] for k in ('id','origin','area','key')},
        'captured_at':source['captured_at'], 'expires_at':expires_at, 'max_age_seconds':age,
        'headers':{k:available[k] for k in selected}, 'header_names':sorted(selected),
        'strategy':strategy, 'identity': None if identity is None else str(identity), 'recipe':copy.deepcopy(recipe)}}


def storage_session_headers(auth, url):
    session = auth.get('session', {})
    try:
        if session['origin'] != origin_of(url): return None, 'session_origin_mismatch'
        maximum_age = session['max_age_seconds']
        if type(maximum_age) is not int or not 1 <= maximum_age <= 30*86400: return None, 'invalid_storage_session'
        if 'identity_pointer' in session['recipe'] and session.get('identity') != session['recipe'].get('identity_expected'):
            return None, 'session_identity_mismatch'
        now = time.time(); age = now - _epoch(session['captured_at'])
        if age < -300 or age > session['max_age_seconds']: return None, 'session_capture_expired'
        if _epoch(session['expires_at']) <= now: return None, 'storage_token_expired'
        headers = dict(session['headers'])
        if set(headers) != set(session['header_names']) or any(
                k in TRANSPORT_HEADERS | {'authorization'} or not re.fullmatch('[a-z0-9-]+', k)
                or not isinstance(v, str) or '\r' in v or '\n' in v for k,v in headers.items()):
            return None, 'invalid_session_headers'
        strategy = session['strategy']
        if strategy['type'] == 'bearer':
            if not isinstance(strategy['token'], str) or not strategy['token'] or any(c in strategy['token'] for c in '\r\n'):
                return None, 'invalid_storage_session'
            headers['authorization'] = 'Bearer '+strategy['token']
        else: return None, 'unsupported_storage_strategy'
        return headers, None
    except (KeyError, TypeError, ValueError, AttributeError): return None, 'invalid_storage_session'
