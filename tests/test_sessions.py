import copy
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx
import pytest

from web2mcp.client import Client, CallError
from web2mcp.discover import discover
from web2mcp.runtime import refresh_credentials
from web2mcp.sessions import cookie_matches
from web2mcp.storage import save_private, load_json

ORIGIN = 'https://session.example'


def capture(value='session-one', identity='alice'):
    now = datetime.now(timezone.utc).isoformat()
    cookies = [
        {'name': 'sid', 'value': value, 'domain': 'session.example', 'path': '/', 'secure': True, 'httpOnly': True, 'expires': time.time()+3600},
        {'name': 'csrf', 'value': 'csrf-fixture', 'domain': 'session.example', 'path': '/', 'secure': True},
        {'name': 'other', 'value': 'never-copy', 'domain': 'unrelated.example', 'path': '/'},
    ]
    return {'format': 'web2mcp-capture-v1', 'captured_at': now, 'cookies': cookies,
            'sessions': {ORIGIN: {'captured_at': now, 'cookies': cookies}},
            'exchanges': [{'request': {'method': method, 'url': ORIGIN+path,
                'headers': {'x-site-request': 'observed-header', 'x-csrftoken': 'csrf-fixture'},
                'postData': None}, 'response': {'status': 200, 'body': json.dumps({'authenticated': True, 'user': identity})}}
                for method, path in [('GET', '/me'), ('POST', '/read'), ('POST', '/write')]]}


def policy():
    return {method+' '+path: {'name': name, 'read_only': name != 'write',
            'authentication': {'type': 'cookie_session', 'required_cookie_names': ['sid']},
            'session_validation': [{'pointer': '/authenticated', 'expected': True}]}
            for method,path,name in [('GET','/me','me'),('POST','/read','read'),('POST','/write','write')]}


@pytest.fixture
def session(tmp_path):
    path = tmp_path/'capture.json'; save_private(path, capture())
    profile = tmp_path/'work'/'profile'; discover(path, profile, [ORIGIN], policy())
    state = {'calls': [], 'status': 200, 'authenticated': True, 'identity': 'alice', 'token': 'session-one'}
    def handler(request):
        state['calls'].append(request)
        assert 'sid='+state['token'] in request.headers['cookie']
        assert 'other=' not in request.headers['cookie']
        assert request.headers['x-site-request'] == 'observed-header'
        return httpx.Response(state['status'], json={'authenticated':state['authenticated'],'user':state['identity']})
    transport = httpx.MockTransport(handler)
    with Client(profile, transport=transport) as client:
        yield client, state, transport


def test_discovery_private_metadata_and_replay(session):
    client,state,_=session
    auth=client.credentials['auth'][client.operations['me']['credential_ref']]
    assert auth['type']=='cookie_session'
    assert auth['session']['cookie_names']==['csrf','sid']
    assert auth['session']['captured_at'] and auth['session']['header_names']
    assert client.call('me')['data']['authenticated']
    for name in ['manifest.json','openapi.json','client.py','server.py','mcp.json']:
        assert 'session-one' not in (client.root/name).read_text()
    assert (client.root/'credentials.json').stat().st_mode & 0o777 == 0o600
    assert 'session-one' not in (client.root/'audit/calls.jsonl').read_text()


@pytest.mark.parametrize('status', [401,403])
def test_auth_refusal_requires_relay_without_retry(session,status):
    client,state,_=session;state['status']=status
    with pytest.raises(CallError) as caught:client.call('me')
    assert caught.value.reason=='session_relay_required'
    assert caught.value.status==status and len(state['calls'])==1
    assert load_json(client.root/'cards/login.json')['automatic_replay'] is False


def test_anonymous_200_not_success(session):
    client,state,_=session;state['authenticated']=False
    with pytest.raises(CallError) as caught:client.call('me')
    assert caught.value.reason=='session_relay_required' and len(state['calls'])==1


@pytest.mark.parametrize('change', ['expired_cookie','expired_capture','origin','partition'])
def test_local_expiry_and_scope_prevent_dispatch(session,change):
    client,state,_=session
    auth=client.credentials['auth'][client.operations['me']['credential_ref']]['session']
    if change=='expired_cookie': auth['cookies'][0]['expires']=1
    if change=='expired_capture': auth['captured_at']='2000-01-01T00:00:00Z'
    if change=='origin': auth['origin']='https://other.example'
    if change=='partition': auth['cookies'][0]['partitionKey']={'topLevelSite':ORIGIN}
    with pytest.raises(CallError) as caught:client.call('me')
    assert caught.value.reason=='session_relay_required' and not state['calls']


def test_cookie_rfc_scope():
    cookie={'name':'sid','value':'x','domain':'session.example','path':'/api','secure':True}
    assert cookie_matches(cookie,ORIGIN+'/api/item')
    for url in ['https://sub.session.example/api','https://session.example.evil/api',ORIGIN+'/apix','http://session.example/api']:
        assert not cookie_matches(cookie,url)
    cookie['domain']='.session.example'
    assert cookie_matches(cookie,'https://sub.session.example/api')
    cookie['hostOnly']=True
    assert not cookie_matches(cookie,'https://sub.session.example/api')


def test_read_post_csrf_and_write_preview_semantics_unchanged(session):
    client,state,_=session
    client.operations['read']['csrf']={'header':'x-csrftoken','cookie':'csrf'}
    client.call('read')
    assert state['calls'][-1].headers['x-csrftoken']=='csrf-fixture'
    assert client.call('write')['preview'] and len(state['calls'])==1
    client.call('write',confirm=True)
    assert len(state['calls'])==2


def test_refresh_same_identity_keeps_manifest_and_checks_new_session(session):
    client,state,transport=session
    save_private(client.root/'runtime.json',{'identity_probe':{'operation':'me','pointer':'/data/user','expected':'alice'}})
    before=(client.root/'manifest.json').read_bytes()
    fresh=client.root.parent/'fresh.json';save_private(fresh,capture('session-two'))
    state['token']='session-two'
    result=refresh_credentials(client.root,fresh,transport=transport)
    assert result['refreshed'] and (client.root/'manifest.json').read_bytes()==before
    assert load_json(client.root/'cards/login.json')['state']=='resolved'
    assert 'session-two' in (client.root/'credentials.json').read_text()
    assert 'session-two' not in (client.root/'audit/calls.jsonl').read_text()


def test_refresh_wrong_identity_does_not_replace_credentials(session):
    client,state,transport=session
    save_private(client.root/'runtime.json',{'identity_probe':{'operation':'me','pointer':'/data/user','expected':'alice'}})
    before=(client.root/'credentials.json').read_bytes()
    fresh=client.root.parent/'fresh.json';save_private(fresh,capture('session-two','bob'))
    state.update(token='session-two',identity='bob')
    with pytest.raises(CallError):refresh_credentials(client.root,fresh,transport=transport)
    assert (client.root/'credentials.json').read_bytes()==before


def test_capture_type_does_not_silently_override_mac(tmp_path):
    data=capture()
    data['exchanges'][0]['request']['headers']['authorization']='MAC id="unknown",nonce="1",mac="x"'
    path=tmp_path/'capture.json';save_private(path,data)
    with pytest.raises(ValueError,match='signature'):discover(path,tmp_path/'p',[ORIGIN],policy())


def test_session_redirect_does_not_forward_credentials(session):
    client,state,_=session;state['status']=302
    with pytest.raises(CallError) as caught:client.call('me')
    assert caught.value.reason=='session_relay_required' and len(state['calls'])==1


def test_cookie_session_filters_private_headers_and_rejects_basic(tmp_path):
    data=capture();data['exchanges'][0]['request']['headers']['authorization']='Basic cGFzc3dvcmQ='
    path=tmp_path/'capture.json';save_private(path,data)
    with pytest.raises(ValueError,match='temporary bearer'):discover(path,tmp_path/'profile',[ORIGIN],policy())


def test_filter_eq_excludes_non_pins_and_ads():
    from web2mcp.expressions import bind
    rows=[{'type':'story'},{'type':'pin','is_promoted':True},{'type':'pin','is_promoted':False,'id':'valid'}, {'type':'pin','is_promoted':0}]
    expr={'$fn':'filter_eq','args':[{'$fn':'filter_eq','args':[rows,'type','pin']},'is_promoted',False]}
    assert bind(expr,{}, {})==[rows[2]]
    with pytest.raises(CallError):bind({'$fn':'filter_eq','args':[[{}]*1001,'type','pin']},{},{})


def test_autopilot_redacts_session_values_not_origin_metadata(tmp_path):
    from web2mcp.autopilot import ExplorerDriver
    driver=ExplorerDriver(tmp_path)
    save_private(tmp_path/'exploration/session.json',{})
    save_private(tmp_path/'exploration/draft/credentials.json',{'auth':{'one':{'type':'cookie_session','session':{
        'origin':ORIGIN,'captured_at':'2026-10-25T00:00:00Z','headers':{'x-auth':'private-header'},
        'cookies':[{'name':'private-cookie','value':'private-session'}]}}}})
    assert set(driver.secrets())=={'private-header','private-session'}


@pytest.mark.parametrize('selector,fields', [('input[type=password]',[]),('input[name=anything]',[{'type':'password'}])])
def test_password_input_rejected_before_persistence(tmp_path,selector,fields):
    from web2mcp.explore import start_session,explore_round
    start_session(tmp_path,ORIGIN,'Relay already logged-in session')
    if fields:save_private(tmp_path/'rounds/001.output.json',{'fields':fields})
    def never(*args,**kwargs):raise AssertionError('Browser must not run')
    with pytest.raises(CallError) as caught:
        explore_round(tmp_path,{'kind':'actions','steps':[{'type':'fill','selector':selector,'value':'must-not-save',
            'purpose':'login','reviewed':True}]},runner=never)
    assert caught.value.reason=='login'
    assert not any('must-not-save' in p.read_text() for p in tmp_path.rglob('*.json'))


def test_numeric_truth_is_not_authenticated_boolean(session):
    client,state,_=session;state['authenticated']=1
    with pytest.raises(CallError) as caught:client.call('me')
    assert caught.value.reason=='session_relay_required'
