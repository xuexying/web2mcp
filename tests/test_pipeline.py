import asyncio
import copy
import hashlib
import hmac
import base64
import importlib.util
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from web2mcp import Client
from web2mcp.client import CallError
from web2mcp.discover import discover, normalize_capture
from web2mcp.storage import save_private, load_json


@pytest.fixture
def api():
    state = {"writes": 0, "requests": []}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            state['requests'].append(self.path)
            if self.headers.get('Authorization') != 'Bearer fixture-secret':
                self.send_response(401); self.end_headers(); return
            u = urlsplit(self.path)
            if u.path == '/redirect':
                self.send_response(302); self.send_header('Location','https://example.com/'); self.end_headers();return
            body = json.dumps({'items':[{'city':parse_qs(u.query).get('city',['北京'])[0]}], 'count':1}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(body)
        def do_POST(self):
            data=json.loads(self.rfile.read(int(self.headers['content-length'])))
            state['writes']+=1
            body=json.dumps({'ok':True,'record':data}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    yield f'http://127.0.0.1:{server.server_port}',state
    server.shutdown();server.server_close();thread.join()


def capture(origin):
    return {'format':'web2mcp-capture-v1','cookies':[], 'exchanges':[
        {'request':{'method':'GET','url':origin+'/customers?city=%E4%B8%8A%E6%B5%B7&limit=20','headers':{'Authorization':'Bearer fixture-secret'}},
         'response':{'status':200,'body':json.dumps({'items':[{'city':'上海'}],'count':1})}},
        {'request':{'method':'POST','url':origin+'/approve','headers':{'Authorization':'Bearer fixture-secret','Content-Type':'application/json'},'postData':json.dumps({'id':12,'decision':'approve'})},
         'response':{'status':200,'body':'{"ok":true}'}},
    ]}


@pytest.fixture
def profile(tmp_path,api):
    origin,state=api
    source=tmp_path/'capture.json';save_private(source,capture(origin))
    dest=tmp_path/'profile'
    discover(source,dest,[origin],{'GET /customers':{'name':'search_customers'},'POST /approve':{'name':'approve'}})
    return dest,state


def test_generated_client_changes_parameter_without_browser(profile):
    root,state=profile
    spec=importlib.util.spec_from_file_location('generated_client',root/'client.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    with module.GeneratedClient() as c:
        result=c.search_customers(query={'city':'北京','limit':2})
    assert result['data']['items'][0]['city']=='北京'
    assert 'limit=2' in state['requests'][-1]


def test_write_preview_and_one_explicit_submission(profile):
    root,state=profile
    with Client(root) as c:
        assert c.call('approve',{'body':{'id':13,'decision':'approve'}})['preview']
        assert state['writes']==0
        result=c.call('approve',{'body':{'id':13,'decision':'approve'}},confirm=True)
        assert result['data']['record']['id']==13
        assert state['writes']==1


def test_unknown_query_rejected_before_network(profile):
    root,state=profile
    with Client(root) as c:
        with pytest.raises(CallError,match='Invalid arguments'):
            c.call('search_customers',{'query':{'invented':'x'}})
    assert not state['requests']


def test_scope_guard(profile):
    root,state=profile
    with Client(root) as c:
        c.operations['search_customers']['url']='https://unrecorded.example/customers'
        with pytest.raises(CallError,match='outside'):
            c.call('search_customers')
    assert not state['requests']


def test_expired_auth_and_redirect(profile):
    root,state=profile
    with Client(root) as c:
        ref=c.operations['search_customers']['credential_ref']
        c.credentials['auth'][ref]['value']='Bearer expired'
        with pytest.raises(CallError,match='AUTH_REQUIRED'):
            c.call('search_customers')
        c.credentials['auth'][ref]['value']='Bearer fixture-secret'
        c.operations['search_customers']['url']=c.operations['search_customers']['url'].replace('/customers','/redirect')
        with pytest.raises(CallError,match='Redirect blocked'):
            c.call('search_customers')
    assert len(state['requests'])==2


def test_no_credentials_in_generated_spec_or_code(profile):
    root,_=profile
    for name in ['manifest.json','openapi.json','client.py','server.py','mcp.json']:
        assert 'fixture-secret' not in (root/name).read_text()
    assert (root/'credentials.json').stat().st_mode & 0o777 == 0o600


def test_har_import():
    value={'log':{'entries':[{'request':{'method':'GET','url':'https://example.org/a','headers':[]},
        'response':{'status':200,'content':{'mimeType':'application/json','text':base64.b64encode(b'{"a":1}').decode(),'encoding':'base64'}}}]}}
    out=normalize_capture(value)
    assert out['exchanges'][0]['response']['body']=='{"a":1}'




def test_unknown_mac_is_not_replayed(tmp_path):
    value=capture('https://example.org')
    value['exchanges'][0]['request']['headers']['Authorization']='MAC id="unknown",nonce="123",mac="wrong"'
    source=tmp_path/'capture.json';save_private(source,value)
    discover(source,tmp_path/'p',['https://example.org'],{'GET /customers':{'name':'search_customers'}})
    with Client(tmp_path/'p') as c:
        with pytest.raises(CallError,match='adapter'):
            c.call('search_customers')


def test_real_official_mcp_client_roundtrip(profile):
    root,state=profile
    async def scenario():
        params=StdioServerParameters(command=sys.executable,args=[str(root/'server.py')])
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:
                init=await session.initialize()
                assert init.serverInfo.name=='web2mcp-local'
                tools=await session.list_tools()
                assert {t.name for t in tools.tools}=={'search_customers','approve'}
                result=await session.call_tool('search_customers',{'query':{'city':'深圳'}})
                assert not result.isError
                assert json.loads(result.content[0].text)['data']['items'][0]['city']=='深圳'
                preview=await session.call_tool('approve',{'body':{'id':99,'decision':'approve'}})
                assert json.loads(preview.content[0].text)['preview']
                assert state['writes']==0
                result=await session.call_tool('approve',{'body':{'id':99,'decision':'approve'},'_confirm':True})
                assert not result.isError
                assert state['writes']==1
    asyncio.run(scenario())


def test_form_post_and_repeated_query(tmp_path):
    value=capture('https://example.org');req=value['exchanges'][1]['request']
    req['headers']['Content-Type']='application/x-www-form-urlencoded'
    req['postData']='id=1&tag=a&tag=b'
    value['exchanges'][0]['request']['url']='https://example.org/customers?tag=a&tag=b&empty='
    source=tmp_path/'capture.json';save_private(source,value)
    discover(source,tmp_path/'p',['https://example.org'],{'GET /customers':{'name':'search'},'POST /approve':{'name':'form'}})
    seen=[]
    def handler(r):
        seen.append(r);return httpx.Response(200,json={'ok':True})
    with Client(tmp_path/'p',transport=httpx.MockTransport(handler)) as c:
        c.call('search');c.call('form',confirm=True)
    assert seen[0].url.params.get_list('tag')==['a','b']
    assert seen[0].url.params['empty']==''
    assert parse_qs(seen[1].content.decode())['tag']==['a','b']


def test_http_errors_have_private_diagnostics_without_public_response_body(tmp_path):
    value=capture('https://example.org');source=tmp_path/'capture.json';save_private(source,value)
    discover(source,tmp_path/'p',['https://example.org'],{'GET /customers':{'name':'search'}})
    def handler(r):
        return httpx.Response(400,json={'message':'sensitive business diagnostic'})
    with Client(tmp_path/'p',transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(CallError,match='private diagnostic') as error:
            c.call('search')
    assert 'sensitive business diagnostic' not in str(error.value)
    records=list((tmp_path/'p'/'diagnostics').glob('*.json'))
    assert len(records)==1 and records[0].stat().st_mode & 0o777 == 0o600
    assert load_json(records[0])['response']['message']=='sensitive business diagnostic'


def test_har_import_preserves_login_cookies(tmp_path):
    # A HAR from a logged-in site (auth via Cookie header) must yield a usable cookie_session,
    # not get its cookies dropped — otherwise HAR import is useless for authenticated sites.
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    har = {'log': {'entries': [{
        'startedDateTime': now,
        'request': {'method': 'GET', 'url': 'https://api.example.com/v0/me',
            'headers': [{'name': 'Cookie', 'value': 'sid=secret-session; theme=dark'},
                        {'name': 'User-Agent', 'value': 'Mozilla/5.0'}]},
        'response': {'status': 200, 'content': {'mimeType': 'application/json', 'text': '{"id":1}'},
            'cookies': [{'name': 'sid', 'value': 'secret-session', 'domain': '.example.com',
                         'path': '/', 'httpOnly': True, 'secure': True}]}}]}}
    cap = normalize_capture(har)
    snapshot = cap['sessions']['https://api.example.com']
    assert {c['name'] for c in snapshot['cookies']} == {'sid', 'theme'}
    assert snapshot['captured_at'][:4] != '1970'
    sid = next(c for c in snapshot['cookies'] if c['name'] == 'sid')
    assert sid['domain'] == '.example.com' and sid['hostOnly'] is False and sid['secure'] is True

    source = tmp_path / 'cap.json'; source.write_text(json.dumps(cap))
    discover(source, tmp_path / 'p', ['https://api.example.com'], {'GET /v0/me': {'name': 'get_me'}})
    creds = json.loads((tmp_path / 'p' / 'credentials.json').read_text())
    sessions = [a['session'] for a in creds['auth'].values() if a.get('type') == 'cookie_session']
    assert sessions, 'HAR login did not become a cookie_session'
    assert 'sid' in sessions[0]['cookie_names']
    assert sessions[0]['captured_at'][:4] != '1970'


def test_discover_read_only_safe(tmp_path):
    # One-step safe profile: reads exposed, writes hidden AND blocked.
    value = {'format': 'web2mcp-capture-v1', 'cookies': [], 'exchanges': [
        {'request': {'method': 'GET', 'url': 'https://example.org/items', 'headers': {}},
         'response': {'status': 200, 'mimeType': 'application/json', 'body': '[{"id":1}]'}},
        {'request': {'method': 'POST', 'url': 'https://example.org/delete_all',
                     'headers': {'content-type': 'application/json'}, 'postData': '{}'},
         'response': {'status': 200, 'mimeType': 'application/json', 'body': '{"ok":true}'}}]}
    source = tmp_path / 'cap.json'; source.write_text(json.dumps(value))
    prof = tmp_path / 'p'
    discover(source, prof, ['https://example.org'],
             {'GET /items': {'name': 'list_items'},
              'POST /delete_all': {'name': 'delete_all', 'read_only': False}},
             read_only_safe=True)
    manifest = json.loads((prof / 'manifest.json').read_text())
    assert manifest['read_only_mode'] is True
    assert manifest['allowed_methods'] == ['GET', 'HEAD']
    by_name = {o['name']: o for o in manifest['operations']}
    assert by_name['delete_all'].get('internal') is True
    assert not by_name['list_items'].get('internal')
