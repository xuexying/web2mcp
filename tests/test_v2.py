import copy
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from web2mcp.audit import Audit, purge_diagnostics
from web2mcp.client import Client, CallError
from web2mcp.contracts import ContractEngine, install_contract
from web2mcp.discover import discover, scoped_cookies
from web2mcp.evidence import infer_schema
from web2mcp.explore import start_session, explore_round, propose
from web2mcp.runtime import health, refresh_credentials, trial_read, publish_reads
from web2mcp.storage import load_json, save_private

ORIGIN='https://fixture.example'


def exchange(method,path,data,body=None,token='Bearer fixture-old'):
    return {'request':{'method':method,'url':ORIGIN+path,'headers':{'Authorization':token,'Content-Type':'application/json'},
                       'postData':json.dumps(body) if body is not None else None},
            'response':{'status':200,'body':json.dumps(data)},'label':'Observed fixture action'}


def row(ident,kind='purchase'):
    return {'id':str(ident),'kind':kind,'status':'pending','version':1}


def capture(token='Bearer fixture-old'):
    items=[exchange('GET','/api/me',{'id':'alice'},token=token)]
    for ident,kind in [('101','purchase'),('102','purchase'),('201','leave')]:
        items.append(exchange('GET','/api/records/'+ident,row(ident,kind),token=token))
        field='budget_code' if kind=='purchase' else 'cover_person'
        items.append(exchange('POST','/api/records/'+ident+'/decision',{'ok':True},
                              {'version':1,'decision':'approved','note':'observed',field:'sample'},token=token))
    return {'format':'web2mcp-capture-v1','exchanges':items,'cookies':[]}


def contract(kind='purchase'):
    field='budget_code' if kind=='purchase' else 'cover_person'
    path={'path':{'record_id':{'$arg':'/id'}}}
    schema={'type':'object','properties':{'id':{'type':'string'},'note':{'type':'string','minLength':1},field:{'type':'string','minLength':1}},'required':['id','note',field],'additionalProperties':False}
    return {'version':1,'name':kind+'_approve','description':'Approve fixture '+kind,'input_schema':schema,
      'lock_key':{'$arg':'/id'},
      'preflight':[{'id':'before','operation':'detail','arguments':path,'pin':['/data/id','/data/version','/data/status','/data/kind'],
        'assertions':[{'pointer':'/data/id','value':{'$arg':'/id'}},{'pointer':'/data/kind','value':kind},{'pointer':'/data/status','value':'pending'}]}],
      'write':{'operation':'decide','allowed_body_fields':['version','decision','note',field],
        'arguments':{**path,'body':{'version':{'$step':'before','pointer':'/data/version'},'decision':'approved','note':{'$arg':'/note'},field:{'$arg':'/'+field}}}},
      'verify':[{'id':'after','operation':'detail','arguments':path,'assertions':[
        {'pointer':'/data/id','value':{'$arg':'/id'}},{'pointer':'/data/status','value':'approved'},
        {'pointer':'/data/version','op':'gt','value':{'$step':'before','pointer':'/data/version'}},
        {'pointer':'/data/note','value':{'$arg':'/note'}},{'pointer':'/data/'+field,'value':{'$arg':'/'+field}}]}]}


@pytest.fixture
def fixture(tmp_path):
    cap=tmp_path/'capture.json';save_private(cap,capture())
    root=tmp_path/'profile'
    policy={'GET /api/me':{'name':'me'},'GET /api/records/{record_id}':{'name':'detail'},
            'POST /api/records/{record_id}/decision':{'name':'decide','read_only':False}}
    discover(cap,root,[ORIGIN],policy)
    state={'rows':{'101':row(101),'102':row(102),'201':row(201,'leave')},'calls':0,'writes':0,'mode':'ok','token':'Bearer fixture-old','identity':'alice'}
    def handle(request):
        state['calls']+=1
        if request.headers.get('authorization')!=state['token']: return httpx.Response(401,json={})
        if state['mode']=='deny':return httpx.Response(403,json={})
        if request.url.path=='/api/me':return httpx.Response(200,json={'id':state['identity']})
        ident=request.url.path.split('/')[3];r=state['rows'][ident]
        if request.method=='GET':
            if state['mode']=='drift': return httpx.Response(200,json={'id':{'changed':'type'}})
            return httpx.Response(200,json=copy.deepcopy(r))
        state['writes']+=1
        if state['mode']=='timeout': raise httpx.ReadTimeout('fixture timeout',request=request)
        if state['mode']!='fake_success':
            data=json.loads(request.content);r.update(data);r['status']=data['decision'];r['version']+=1
        return httpx.Response(200,json={'ok':True})
    transport=httpx.MockTransport(handle)
    client=Client(root,transport=transport)
    yield client,state,transport,cap
    client.close()


def test_observations_are_not_required_or_enums():
    s=infer_schema([{'a':1,'b':None},{'a':1,'c':'x'}])
    assert 'required' not in s and 'enum' not in s['properties']['a']
    assert s['x-observation']['always_present_candidates']==['a']
    assert set(s['properties'])=={'a','b','c'}


def test_status_reconciles_late_write_without_replay(fixture):
    c,state,_,_=fixture
    spec=contract();spec['verify_on_status']=True
    install_contract(c,spec,reviewed=True);engine=ContractEngine(c)
    args={'id':'101','note':'authorized','budget_code':'known'}
    plan=engine.prepare('purchase_approve',args)
    state['mode']='timeout'
    with pytest.raises(CallError):engine.submit('purchase_approve',plan['preview_id'])
    assert state['writes']==1
    assert engine.status('purchase_approve',plan['preview_id'])['readback_verified'] is False
    state['rows']['101'].update(status='approved',version=2,note='authorized',budget_code='known')
    result=engine.status('purchase_approve',plan['preview_id'])
    assert result['state']=='verified' and result['readback_verified'] is True
    assert state['writes']==1
    with pytest.raises(CallError):engine.submit('purchase_approve',plan['preview_id'])
    assert state['writes']==1


def test_status_does_not_submit_unattempted_plan_or_use_other_preview(fixture):
    c,state,_,_=fixture
    spec=contract();spec['verify_on_status']=True
    install_contract(c,spec,reviewed=True);engine=ContractEngine(c)
    a=engine.prepare('purchase_approve',{'id':'101','note':'first','budget_code':'known'})
    b=engine.prepare('purchase_approve',{'id':'101','note':'second','budget_code':'known'})
    assert engine.status('purchase_approve',a['preview_id'])['state']=='not_submitted'
    engine.submit('purchase_approve',a['preview_id'])
    r=engine.status('purchase_approve',b['preview_id'])
    assert r['preview_id']==a['preview_id']
    assert r['readback_error']['reason']=='another_preview_owns_submission'
    assert state['writes']==1


def test_status_does_not_report_stale_success_after_readback_fails(fixture):
    c,state,_,_=fixture
    spec=contract();spec['verify_on_status']=True
    install_contract(c,spec,reviewed=True);engine=ContractEngine(c)
    p=engine.prepare('purchase_approve',{'id':'101','note':'authorized','budget_code':'known'})
    engine.submit('purchase_approve',p['preview_id'])
    state['rows']['101']['status']='changed elsewhere'
    r=engine.status('purchase_approve',p['preview_id'])
    assert r['state']=='needs_check' and r['previous_state']=='verified'
    assert r['business_verified'] is False and r['readback_verified'] is False
    assert state['writes']==1


def test_merges_samples_and_id_paths_but_not_action_paths(fixture):
    c,_,_,_=fixture
    assert c.operations['detail']['samples']==3
    assert c.operations['detail']['url'].endswith('/{record_id}')
    assert set(c.operations['decide']['input_schema']['properties']['body']['properties']) >= {'budget_code','cover_person'}
    cap=capture();cap['exchanges'].append(exchange('GET','/api/records/search',{'items':[]}))
    save_private(c.root/'more.json',cap);discover(c.root/'more.json',c.root/'more',[ORIGIN])
    ops=load_json(c.root/'more'/'manifest.json')['operations']
    assert any(o['url'].endswith('/search') for o in ops)


def test_cookie_domain_path_and_secure_scope():
    cookies=[{'domain':'fixture.example','path':'/api','name':'yes','value':'1'},
             {'domain':'other.example','path':'/','name':'other','value':'2'},
             {'domain':'fixture.example','path':'/admin','name':'admin','value':'3'},
             {'domain':'fixture.example','path':'/','name':'secure','value':'4','secure':True}]
    assert [c['name'] for c in scoped_cookies(cookies,['http://fixture.example/api/x'])]==['yes']
    assert not scoped_cookies(cookies,['https://child.fixture.example/api/x'])


def test_read_trial_requires_variation_and_assertions(fixture):
    c,*_=fixture
    case={'arguments':{'path':{'record_id':'101'}},'assertions':[{'pointer':'/data/id','value':'101'}]}
    with pytest.raises(CallError,match='different'):trial_read(c,'detail',[case],reviewed_read_only=True)
    with pytest.raises(CallError,match='Every trial'):trial_read(c,'detail',[{'arguments':{}}],reviewed_read_only=True)
    second=copy.deepcopy(case);second['arguments']['path']['record_id']='102';second['assertions'][0]['value']='102'
    assert trial_read(c,'detail',[case,second],reviewed_read_only=True)['state']=='passed'
    assert publish_reads(c,['detail'])['published']==['detail']


def test_trial_catches_wrong_target_and_stale_definition(fixture):
    c,*_=fixture
    with pytest.raises(CallError): trial_read(c,'me',[{'assertions':[{'pointer':'/data/id','value':'bob'}]}],reviewed_read_only=True)
    trial_read(c,'me',[{'assertions':[{'pointer':'/data/id','value':'alice'}]}],reviewed_read_only=True)
    c.operations['me']['description']='changed after trial'
    with pytest.raises(CallError,match='stale'):publish_reads(c,['me'])
    with pytest.raises(CallError,match='Raw writes'):publish_reads(c,['decide'])


def test_strict_drift_stops_and_creates_update_card(fixture):
    c,state,*_=fixture;state['mode']='drift';c.operations['detail']['response_validation']='strict'
    with pytest.raises(CallError) as caught:c.call('detail',{'path':{'record_id':'101'}})
    assert caught.value.reason=='schema_changed'
    card=load_json(next((c.root/'cards').glob('*.json')))
    assert card['type']=='tool_update' and not card['automatic_replay']


def test_auth_permission_are_distinct_and_not_retried(fixture):
    c,state,*_=fixture;state['mode']='deny'
    with pytest.raises(CallError) as error:c.call('me')
    assert error.value.reason=='permission' and not (c.root/'cards'/'login.json').exists()
    state['mode']='ok';state['token']='new'
    with pytest.raises(CallError) as error:c.call('me')
    assert error.value.reason=='login' and state['calls']==2
    assert load_json(c.root/'cards'/'login.json')['state']=='login_required'


def test_audit_omits_arguments_and_detects_tamper(fixture):
    c,*_=fixture;c.call('decide',{'path':{'record_id':'101'},'body':{'note':'private business content'}})
    log=c.root/'audit'/'calls.jsonl'
    assert 'private business content' not in log.read_text() and 'fixture-old' not in log.read_text()
    assert c.audit.verify()['valid']
    log.write_text(log.read_text().replace('preview','tampered'))
    assert not c.audit.verify()['valid']


def test_audit_concurrent_initialization_and_append(tmp_path):
    def append(n):
        a=Audit(tmp_path);a.append(event='test',number=n);return a.arguments_hash({'same':1})
    with ThreadPoolExecutor(max_workers=8) as pool: hashes=list(pool.map(append,range(24)))
    assert len(set(hashes))==1
    assert Audit(tmp_path).verify()['records']==24


def test_two_types_share_contract_executor(fixture):
    c,state,*_=fixture
    for kind,ident,field in [('purchase','101','budget_code'),('leave','201','cover_person')]:
        install_contract(c,contract(kind),reviewed=True)
        engine=ContractEngine(c);name=kind+'_approve'
        p=engine.call(name+'_prepare',{'id':ident,'note':'approved from contract',field:'test-value'})
        assert state['writes']==(kind=='leave')
        result=engine.call(name+'_submit',{'preview_id':p['preview_id'],'confirm':True})
        assert result['business_verified'] and state['rows'][ident][field]=='test-value'
    assert state['writes']==2


def test_contract_state_pin_prevents_stale_write(fixture):
    c,state,*_=fixture;install_contract(c,contract(),reviewed=True);e=ContractEngine(c)
    p=e.prepare('purchase_approve',{'id':'101','note':'n','budget_code':'b'})
    state['rows']['101']['version']=2
    with pytest.raises(CallError,match='state changed'):e.submit('purchase_approve',p['preview_id'])
    assert state['writes']==0


@pytest.mark.parametrize('mode',['fake_success','timeout'])
def test_unknown_or_http_success_without_business_success_never_replays(fixture,mode):
    c,state,*_=fixture;install_contract(c,contract(),reviewed=True);e=ContractEngine(c)
    args={'id':'101','note':'n','budget_code':'b'};p=e.prepare('purchase_approve',args);state['mode']=mode
    with pytest.raises(CallError,match='not safe'):e.submit('purchase_approve',p['preview_id'])
    assert e.status('purchase_approve',p['preview_id'])['state']=='needs_check'
    state['rows']['101']['version']+=1
    p2=e.prepare('purchase_approve',args)
    with pytest.raises(CallError,match='Already attempted'):e.submit('purchase_approve',p2['preview_id'])
    assert state['writes']==1


def test_contract_rejects_extra_write_fields_and_wrong_record_type(fixture):
    c,state,*_=fixture;bad=contract();bad['write']['arguments']['body']['admin']=True
    with pytest.raises(CallError,match='allowed_body_fields'):install_contract(c,bad,reviewed=True)
    install_contract(c,contract(),reviewed=True);e=ContractEngine(c)
    with pytest.raises(CallError,match='check failed'):e.prepare('purchase_approve',{'id':'201','note':'n','budget_code':'b'})
    assert state['writes']==0


def test_contract_changes_need_new_review(fixture):
    c,*_=fixture;install_contract(c,contract(),reviewed=True);e=ContractEngine(c)
    c.operations['decide']['body']['note']='changed'
    with pytest.raises(CallError,match='definitions changed'):e.prepare('purchase_approve',{'id':'101','note':'n','budget_code':'b'})


def test_refresh_preserves_manifest_and_pins_identity(fixture):
    c,state,transport,_=fixture
    config={'identity_probe':{'operation':'me','pointer':'/data/id','expected':'alice'}}
    save_private(c.root/'runtime.json',config)
    before=(c.root/'manifest.json').read_bytes()
    save_private(c.root/'fresh.json',capture('Bearer fixture-new'));state['token']='Bearer fixture-new'
    assert refresh_credentials(c.root,c.root/'fresh.json',transport=transport)['refreshed']
    assert (c.root/'manifest.json').read_bytes()==before
    saved=(c.root/'credentials.json').read_bytes();state['identity']='bob'
    with pytest.raises(CallError,match='identity_mismatch'):refresh_credentials(c.root,c.root/'fresh.json',transport=transport)
    assert (c.root/'credentials.json').read_bytes()==saved


def test_health_missing_probe_is_not_healthy(fixture):
    c,*_=fixture;assert health(c,{})=={'healthy':False,'reason':'probe_not_configured'}


def test_retention_cleans_only_old_diagnostics(fixture):
    c,*_=fixture;old=c.root/'diagnostics'/'old.json';new=c.root/'diagnostics'/'new.json'
    save_private(old,{});save_private(new,{})
    os.utime(old,(time.time()-10*86400,)*2)
    c.call('me')
    assert purge_diagnostics(c.root,7)['removed']==1
    assert new.exists() and (c.root/'audit'/'calls.jsonl').exists()


def test_exploration_budget_scope_and_no_unreviewed_replay(tmp_path):
    start_session(tmp_path,'https://fixture.example/','inspect',budget=1)
    def never(*args,**kwargs):pytest.fail('must block before browser')
    with pytest.raises(CallError):explore_round(tmp_path,{'kind':'actions','steps':[{'type':'click','selector':'button'}]},runner=never)
    with pytest.raises(CallError):explore_round(tmp_path,{'kind':'observe','steps':[{'type':'click'}]},runner=never)
    with pytest.raises(CallError):explore_round(tmp_path,{'kind':'actions','steps':[{'type':'goto','url':'https://elsewhere.example','purpose':'test','reviewed':True}]},runner=never)
    with pytest.raises(CallError):explore_round(tmp_path,{'kind':'resume'},runner=never)


def test_proposed_tools_are_private_candidates_not_published(tmp_path):
    start_session(tmp_path,ORIGIN,'inspect')
    save_private(tmp_path/'capture.json',capture())
    result=propose(tmp_path)
    assert result['published']==0
    manifest=load_json(tmp_path/'draft'/'manifest.json')
    assert all(op['internal'] and op['verification']=='needs_trial' for op in manifest['operations'])
    with pytest.raises(CallError,match='already exists'):propose(tmp_path)


def test_secret_request_body_not_baked_into_generated_tools(tmp_path):
    cap=capture();cap['exchanges'].append(exchange('POST','/api/exchange',{'ok':True},{'nested':{'refresh_token':'secret-value'}}))
    save_private(tmp_path/'cap.json',cap);discover(tmp_path/'cap.json',tmp_path/'profile',[ORIGIN])
    manifest=(tmp_path/'profile'/'manifest.json').read_text()
    assert 'secret-value' not in manifest and '/api/exchange' not in manifest


def test_exploration_round_lock_blocks_parallel_actions(tmp_path):
    import fcntl
    start_session(tmp_path,ORIGIN,'inspect')
    with (tmp_path/'session.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(CallError,match='already running'):explore_round(tmp_path,{'kind':'observe'})


def test_moved_profile_mcp_imports_package_and_hides_internal_tools(fixture,tmp_path):
    import asyncio,shutil,sys
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    c,*_=fixture
    manifest=load_json(c.root/'manifest.json')
    for op in manifest['operations']:op['internal']=op['name']=='decide'
    save_private(c.root/'manifest.json',manifest)
    moved=tmp_path/'new-location';shutil.copytree(c.root,moved)
    assert 'sys.path.insert' not in (moved/'server.py').read_text()
    async def run():
        async with stdio_client(StdioServerParameters(command=sys.executable,args=[str(moved/'server.py')],cwd=str(tmp_path))) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize();listed=await session.list_tools()
                assert {tool.name for tool in listed.tools}=={'detail','me'}
                rejected=await session.call_tool('decide',{'path':{'record_id':'101'},'_confirm':True})
                assert rejected.isError
    asyncio.run(run())


def test_explorer_mcp_lists_control_plane_without_starting_browser(tmp_path):
    import asyncio,sys
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    start_session(tmp_path,ORIGIN,'inspect')
    save_private(tmp_path/'capture.json',capture())
    async def run():
        async with stdio_client(StdioServerParameters(command=sys.executable,args=['-m','web2mcp','serve-explorer',str(tmp_path)])) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize();listed=await session.list_tools()
                names={tool.name:tool for tool in listed.tools}
                assert not names['explorer_act'].annotations.readOnlyHint
                assert names['explorer_observe'].annotations.readOnlyHint
                result=await session.call_tool('explorer_propose',{})
                assert not result.isError and json.loads(result.content[0].text)['published']==0
                invalid=await session.call_tool('explorer_act',{'label':'bad','steps':[]})
                assert invalid.isError
    asyncio.run(run())

def test_contract_verification_polls_reads_without_repeating_write(fixture, monkeypatch):
    c, state, *_ = fixture
    cfg = contract(); cfg.update(verification_attempts=3, verification_interval_seconds=0)
    install_contract(c, cfg, reviewed=True)
    original = c.call; stale_reads = []
    def eventual(name, *args, **kwargs):
        response = original(name, *args, **kwargs)
        if name == 'detail' and state['writes'] and len(stale_reads) < 2:
            stale_reads.append(True)
            response['data']['status'] = 'pending'
        return response
    monkeypatch.setattr(c, 'call', eventual)
    engine = ContractEngine(c)
    plan = engine.prepare('purchase_approve', {'id':'101','note':'checked','budget_code':'B1'})
    result = engine.submit('purchase_approve', plan['preview_id'])
    assert result['business_verified'] and len(stale_reads) == 2
    assert state['writes'] == 1


def test_contract_verification_exhaustion_never_repeats_write(fixture):
    c, state, *_ = fixture
    cfg = contract(); cfg.update(verification_attempts=2, verification_interval_seconds=0)
    install_contract(c, cfg, reviewed=True); state['mode'] = 'fake_success'
    engine = ContractEngine(c)
    plan = engine.prepare('purchase_approve', {'id':'101','note':'checked','budget_code':'B1'})
    with pytest.raises(CallError, match='read-back') as failed:
        engine.submit('purchase_approve', plan['preview_id'])
    assert failed.value.outcome == 'write_response_received'
    receipt = load_json(c.root/'diagnostics'/('contract-'+plan['preview_id']+'.json'))
    assert receipt['response']['status'] == 200 and receipt['response']['data'] == {'ok':True}
    assert state['writes'] == 1
    status = engine.status('purchase_approve', plan['preview_id'])
    assert status['state'] == 'needs_check'
    assert status['write_response_received'] is True
    assert status['error']['outcome'] == 'write_response_received'
    with pytest.raises(CallError, match='Already attempted'):
        engine.submit('purchase_approve', plan['preview_id'])
    assert state['writes'] == 1


@pytest.mark.parametrize('attempts,interval', [(True,1),(0,1),(7,1),(2,-1),(2,6)])
def test_contract_rejects_unbounded_verification(fixture, attempts, interval):
    c, *_ = fixture
    cfg = contract(); cfg.update(verification_attempts=attempts, verification_interval_seconds=interval)
    with pytest.raises(CallError, match='Verification'):
        install_contract(c, cfg, reviewed=True)


def test_bodyless_get_action_still_requires_preview_and_blocks_duplicate(fixture):
    c, state, *_ = fixture
    op = c.operations['decide']; op.update(method='GET', body=None,
        input_schema={'type':'object','properties':{'path':{'type':'object','properties':{'record_id':{'type':'string'}},'required':['record_id'],'additionalProperties':False}},'required':['path'],'additionalProperties':False})
    def handle(request):
        ident = request.url.path.split('/')[3]
        if request.url.path.endswith('/decision'):
            assert request.method == 'GET' and request.content == b''
            state['writes'] += 1
            state['rows'][ident].update(status='approved',version=2)
        return httpx.Response(200,json=state['rows'][ident])
    c.http.close(); c.http = httpx.Client(transport=httpx.MockTransport(handle))
    cfg = contract(); cfg['write'].update(bodyless=True,allowed_body_fields=[])
    cfg['write']['arguments'].pop('body')
    cfg['verify'][0]['assertions'] = cfg['verify'][0]['assertions'][:3]
    install_contract(c,cfg,reviewed=True); engine = ContractEngine(c)
    plan = engine.prepare('purchase_approve',{'id':'101','note':'checked','budget_code':'B1'})
    assert state['writes'] == 0
    assert engine.submit('purchase_approve',plan['preview_id'])['business_verified']
    assert state['writes'] == 1
    with pytest.raises(CallError,match='Already attempted|Contract check failed'):
        engine.submit('purchase_approve',plan['preview_id'])
    assert state['writes'] == 1


@pytest.mark.parametrize('hidden_body', [False, True])
def test_bodyless_contract_rejects_explicit_or_operation_body(fixture,hidden_body):
    c, *_ = fixture; cfg = contract(); cfg['write'].update(bodyless=True,allowed_body_fields=[])
    if hidden_body:
        cfg['write']['arguments'].pop('body')
        c.operations['decide']['body'] = {'unreviewed':'value'}
    with pytest.raises(CallError,match='Bodyless'):
        install_contract(c,cfg,reviewed=True)
