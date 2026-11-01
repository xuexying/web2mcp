import copy
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from web2mcp.client import Client, CallError
from web2mcp.discover import discover
from web2mcp.runtime import refresh_credentials, refresh_storage_credentials
from web2mcp.storage import load_json, save_private
from web2mcp.storage_sessions import storage_auth

ORIGIN='https://api.example'






def fixture_capture(value='one', uid='42', strategy='bearer'):
    now=datetime.now(timezone.utc)
    token={'access_token':value, 'expires_at':(now+timedelta(hours=1)).isoformat(),
           'user_id':uid, 'refresh_token':'do-not-copy'}
    source={'id':'token','origin':'https://web.example','area':'localStorage','key':'auth',
            'captured_at':now.isoformat(),'value':json.dumps({'value':json.dumps(token)})}
    request={'method':'GET','url':ORIGIN+'/me','headers':{'x-app':'example','authorization':'Bearer '+value}}
    return {'format':'web2mcp-capture-v1','storage_sources':[source],'cookies':[],
            'exchanges':[{'request':request,'response':{'status':200,'body':json.dumps({'user_id':'42'})}}]}


def recipe(strategy='bearer'):
    return {'type':'storage_session','source_id':'token','source_origin':'https://web.example',
        'decode':['json',{'pointer':'/value'},'json'],'expires_at_pointer':'/expires_at',
        'identity_pointer':'/user_id','identity_expected':'42','header_names':['x-app'],
        'strategy':{'type':'bearer','token_pointer':'/access_token'}}


def profile(tmp_path,strategy='bearer'):
    cap=tmp_path/'capture.json';save_private(cap,fixture_capture(strategy=strategy))
    dest=tmp_path/'profile';discover(cap,dest,[ORIGIN],{'GET /me':{'name':'me','authentication':recipe(strategy)}})
    m=load_json(dest/'manifest.json');m.update(read_only_mode=True,sensitive_responses=True);save_private(dest/'manifest.json',m)
    save_private(dest/'runtime.json',{'identity_probe':{'operation':'me','pointer':'/data/user_id','expected':'42'}})
    return dest


@pytest.mark.parametrize('strategy',['bearer'])
def test_private_minimum_credentials_and_fresh_headers(tmp_path,strategy):
    p=profile(tmp_path,strategy);calls=[]
    def handler(req):calls.append(req);return httpx.Response(200,json={'user_id':'42'})
    with Client(p,transport=httpx.MockTransport(handler)) as c:
        c.http.cookies.set('sid','unrelated')
        c.call('me');c.call('me')
        assert all('cookie' not in r.headers for r in calls)
        assert all(r.headers['authorization']=='Bearer one' for r in calls)
    assert 'do-not-copy' not in (p/'credentials.json').read_text()
    assert (p/'credentials.json').stat().st_mode&0o777==0o600
    assert all('private-key' not in (p/f).read_text() for f in ['manifest.json','openapi.json','audit/calls.jsonl'])


@pytest.mark.parametrize('change',['expiry','capture_age','scope','identity','age_type','unsupported'])
def test_local_failures_stop_before_network(tmp_path,change):
    p=profile(tmp_path);calls=[]
    with Client(p,transport=httpx.MockTransport(lambda r:calls.append(r))) as c:
        session=next(iter(c.credentials['auth'].values()))['session']
        if change=='expiry':session['expires_at']='2000-01-01T00:00:00Z'
        if change=='capture_age':session['captured_at']='2000-01-01T00:00:00Z'
        if change=='scope':session['origin']='https://wrong.example'
        if change=='identity':session['identity']='99'
        if change=='age_type':session['max_age_seconds']='forever'
        if change=='unsupported':session['strategy']['type']='unknown'
        with pytest.raises(CallError) as e:c.call('me')
        assert e.value.reason=='session_relay_required' and not calls


@pytest.mark.parametrize('status',[401,403,302,500])
def test_auth_and_sensitive_error_no_retry_no_leak(tmp_path,status):
    p=profile(tmp_path);calls=[]
    def handler(req):calls.append(req);return httpx.Response(status,json={'message':'PRIVATE_BODY_NAME'})
    with Client(p,transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(CallError) as e:c.call('me')
        assert len(calls)==1 and e.value.private_response is None
        if status!=500:assert e.value.reason=='session_relay_required'
    for f in p.rglob('*.json*'):assert 'PRIVATE_BODY_NAME' not in f.read_text()


def test_relay_enforces_token_identity_and_manifest_unchanged(tmp_path):
    p=profile(tmp_path);before=(p/'manifest.json').read_bytes()
    fresh=tmp_path/'fresh.json';save_private(fresh,fixture_capture('two'))
    transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'user_id':'42'}))
    assert refresh_credentials(p,fresh,transport=transport)['refreshed']
    assert (p/'manifest.json').read_bytes()==before
    saved=(p/'credentials.json').read_bytes()
    save_private(fresh,fixture_capture('three','99'))
    with pytest.raises(CallError):refresh_credentials(p,fresh,transport=transport)
    assert (p/'credentials.json').read_bytes()==saved




def test_source_and_observed_signature_must_match():
    cap=fixture_capture();request=cap['exchanges'][0]['request'];headers={'x-app':'example'}
    wrong=copy.deepcopy(cap);wrong['storage_sources'][0]['origin']='https://evil.example'
    with pytest.raises(ValueError):storage_auth(request,wrong,headers,recipe())
    request['headers']['authorization']=request['headers']['authorization'].replace('one','other')
    with pytest.raises(ValueError):storage_auth(request,cap,headers,recipe())


@pytest.mark.parametrize('change',['success','identity','source_key','expired','rejected'])
def test_storage_snapshot_renewal_preserves_tools_and_old_credentials_on_failure(tmp_path,change):
    p=profile(tmp_path);before=(p/'credentials.json').read_bytes();manifest=(p/'manifest.json').read_bytes()
    sources=fixture_capture('new-secret','99' if change=='identity' else '42')['storage_sources']
    if change=='source_key':sources[0]['key']='different'
    if change=='expired':sources[0]['captured_at']='2000-01-01T00:00:00Z'
    snap=tmp_path/'storage.json';save_private(snap,{'format':'web2mcp-storage-v1','storage_sources':sources})
    calls=[]
    def handler(r):calls.append(r);return httpx.Response(403 if change=='rejected' else 200,json={'user_id':'42'})
    if change=='success':
        assert refresh_storage_credentials(p,snap,transport=httpx.MockTransport(handler))['refreshed']
        assert 'new-secret' in (p/'credentials.json').read_text() and len(calls)==1
    else:
        with pytest.raises(CallError):refresh_storage_credentials(p,snap,transport=httpx.MockTransport(handler))
        assert (p/'credentials.json').read_bytes()==before
        assert len(calls)==(1 if change=='rejected' else 0)
    assert (p/'manifest.json').read_bytes()==manifest


def test_browser_storage_explicit_keys_nested_password_and_origin(tmp_path):
    node=shutil.which('node')
    if not node:pytest.skip('Node required')
    mod=Path(__file__).resolve().parents[1]/'web2mcp/browser_storage.mjs'
    script=tmp_path/'storage.mjs'
    script.write_text('''
import assert from 'node:assert/strict';
const {captureStorage,saveStorageSnapshot}=await import(process.argv[2]);
const source={id:'token',origin:'https://web.example',area:'sessionStorage',key:'auth'};
let calls=[];let value='{"value":"temporary-token"}';
const page={url:async()=>source.origin+'/app',evaluate:async(fn,arg)=>{calls.push(arg);return value;}};
const result=await saveStorageSnapshot(page,[source],process.argv[3]);
assert.equal(result[0].found,true);assert.equal(calls.length,1);assert.equal(calls[0].key,'auth');
assert(!JSON.stringify(result).includes('temporary-token'));
await assert.rejects(captureStorage(page,[{...source,origin:'https://other.example'}]));
value=JSON.stringify({value:JSON.stringify({password:'do-not-store'})});
await assert.rejects(captureStorage(page,[source]));
''')
    subprocess.run([node,str(script),mod.as_uri(),str(tmp_path/'private/storage.json')],check=True,capture_output=True,timeout=10)
    assert (tmp_path/'private/storage.json').stat().st_mode&0o777==0o600
