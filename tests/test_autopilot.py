"""Autonomous orchestration tests: scripted planners are not evidence of LLM accuracy."""
import asyncio
import copy
import json
from pathlib import Path
import threading
import time
from concurrent.futures import Future
import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from web2mcp.autopilot import IntegrationJobs, ExplorerDriver
from web2mcp.client import CallError
from web2mcp.integration_api import IntegrationAPI, make_server
from web2mcp.planner import ChatCompletionsPlanner, ModelUnavailable
from web2mcp.storage import load_json, save_private
from test_packs import site, EXAMPLES


def action(kind, **args):return {'action':kind,'arguments':args,'reason':'test concrete next step'}
class ScriptedPlanner:
    def __init__(self,actions):self.actions=iter(actions);self.contexts=[]
    def next(self,context):self.contexts.append(context);return next(self.actions)
class StubDriver:
    def __init__(self,root):self.root=Path(root);self.calls=[];self.writes=0;self.closed=False;self.resumed=0;self.handed=0;self.installed=True;self.revision='v1'
    def secrets(self):return ['very-private-session-secret']
    def call(self,name,args):
        self.calls.append((name,args))
        if name=='call_tool' and args['name']=='demo_submit':self.writes+=1;return {'business_verified':True}
        return {'items':[{'id':'a'}]}
    def write_required(self,d):return d['action']=='call_tool' and d['arguments']['name']=='demo_submit'
    def write_preview(self,d):return {'actual_decision':'approve','target':'record-1'}
    def handoff(self):self.handed+=1
    def resume(self):self.resumed+=1
    def finish(self):self.closed=True
    def capabilities(self):return [{'tool':'demo_read','status':'read_trial_passed'}] if self.installed else []
    def tool_revision(self,name):return self.revision
    def config(self):return {'mcpServers':{'fixture':{'command':'python','args':['-m','fixture']}}}
@pytest.fixture
def jobs(tmp_path):
    drivers={}
    def factory(root):return drivers.setdefault(str(root),StubDriver(root))
    jobs=IntegrationJobs(tmp_path/'jobs',driver_factory=factory)
    ident=jobs.create({'url':'https://fixture.example','max_steps':20})['id']
    return jobs,ident,factory(jobs.directory(ident))

def test_missing_model_becomes_resumable_question(jobs):
    j,i,d=jobs;r=j.run(i,ChatCompletionsPlanner({}))
    assert r['status']=='waiting_user' and r['question']['reason']=='model_not_configured'
    assert d.calls==[]
    j.answer(i,r['question']['id'],'模型配置已补充')
    assert j.run(i,ScriptedPlanner([action('observe'),action('finish',summary='done',remaining=[])]))['status']=='ready'

def test_login_answer_keeps_job_and_browser(jobs):
    j,i,d=jobs;r=j.run(i,ScriptedPlanner([action('ask_user',reason='login',question='请登录')]))
    assert d.handed==1
    with pytest.raises(CallError):j.answer(i,r['question']['id'],'好了')
    j.answer(i,r['question']['id'],'已登录',login_completed=True)
    result=j.run(i,ScriptedPlanner([action('observe'),action('finish',summary='done',remaining=[])]))
    assert result['id']==i and d.resumed==1 and result['status']=='ready'

def test_approval_is_bound_to_concrete_action_and_consumed(jobs):
    j,i,d=jobs;write=action('call_tool',name='demo_submit',arguments={'preview_id':'p','confirm':True})
    r=j.run(i,ScriptedPlanner([write]));assert d.writes==0
    assert r['question']['detail']['target']=='record-1'
    with pytest.raises(CallError):j.answer(i,'old-question','yes',approved=True)
    j.answer(i,r['question']['id'],'执行本次预览',approved=True)
    # The approved exact action runs without asking the model to invent another action.
    r=j.run(i,ScriptedPlanner([write]));assert d.writes==1
    assert r['status']=='waiting_user' and r['question']['reason']=='write_authorization'
    assert j.load(i)['grants']==[]

def test_decline_does_not_authorize_write(jobs):
    j,i,d=jobs;r=j.run(i,ScriptedPlanner([action('call_tool',name='demo_submit',arguments={})]))
    j.answer(i,r['question']['id'],'不执行',approved=False)
    j.run(i,ScriptedPlanner([action('finish',summary='read only',remaining=['Write not tested'])]))
    assert d.writes==0

def test_interrupted_dispatch_is_not_replayed(jobs):
    j,i,d=jobs;state=j.load(i);state['status']='running';state['pending_action']=action('call_tool',name='demo_submit',arguments={});j.save(state)
    r=j.run(i,ScriptedPlanner([]));assert r['question']['reason']=='interrupted' and d.writes==0

def test_uncertain_write_stops_after_one_attempt(jobs):
    j,i,d=jobs;write=action('call_tool',name='demo_submit',arguments={})
    r=j.run(i,ScriptedPlanner([write]));j.answer(i,r['question']['id'],'允许',approved=True)
    original=d.call
    def failure(name,args):original(name,args);raise CallError('Network timeout',reason='network',outcome='unknown')
    d.call=failure;r=j.run(i,ScriptedPlanner([]))
    assert d.writes==1 and r['question']['reason']=='uncertain_effect'

def test_invalid_planner_action_never_executes_and_errors_bounded(jobs):
    j,i,d=jobs;bad={'action':'shell','arguments':{'cmd':'anything'},'reason':'bad'}
    r=j.run(i,ScriptedPlanner([bad,bad,bad]));assert r['status']=='waiting_user' and not d.calls

def test_no_tools_cannot_be_published_as_success(jobs):
    j,i,d=jobs;d.installed=False
    r=j.run(i,ScriptedPlanner([action('finish',summary='everything works',remaining=[])]*3))
    assert r['status']=='waiting_user' and not d.closed and not r.get('result')

def test_secrets_redacted_before_model(jobs):
    j,i,d=jobs;state=j.load(i);state['history']=[{'observation':{'authorization':'Bearer password','text':'prefix very-private-session-secret suffix'}}]
    value=json.dumps(j.context(state,d));assert 'very-private-session-secret' not in value and 'Bearer password' not in value

def test_pause_and_bounded_resume(jobs):
    j,i,d=jobs;save_private(j.directory(i)/'pause-request.json',{})
    assert j.run(i,ScriptedPlanner([]))['status']=='paused'
    j.resume(i);state=j.load(i);state['steps']=state['max_steps'];j.save(state)
    assert j.run(i,ScriptedPlanner([]))['status']=='budget_exhausted'
    with pytest.raises(CallError):j.resume(i)
    assert j.resume(i,extra_steps=4)['max_steps']==24

def test_request_rejects_secret_urls_and_bad_ids(jobs):
    j,i,d=jobs
    for url in ['file:///etc/passwd','https://user:password@example.com','https://example.com/?access_token=private']:
        with pytest.raises(CallError):j.create({'url':url})
    with pytest.raises(CallError):j.get('../../secret')

def test_obvious_ui_submission_cannot_be_mislabeled_read_only(tmp_path):
    d=ExplorerDriver(tmp_path)
    assert d.write_required(action('act',effect='navigation',label='foo',steps=[{'type':'click','selector':'button[type=submit]','purpose':'continue'}]))
    assert not d.write_required(action('act',effect='navigation',label='open tab',steps=[{'type':'click','selector':'a[href="/history"]','purpose':'查看历史'}]))

def test_model_wire_format_and_no_key_in_context(monkeypatch):
    monkeypatch.setenv('TEST_MODEL_SECRET','private-model-key')
    def handle(request):
        assert request.headers['Authorization']=='Bearer private-model-key'
        body=json.loads(request.content)
        assert body['model']=='user-selected-model' and body['response_format']=={'type':'json_object'}
        assert 'private-model-key' not in request.content.decode()
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(action('observe'))}}]})
    planner=ChatCompletionsPlanner({'endpoint':'https://model.example/v1/chat/completions','model':'user-selected-model','api_key_env':'TEST_MODEL_SECRET'},transport=httpx.MockTransport(handle))
    assert planner.next({'url':'https://site.example'})['action']=='observe'

class ImmediateExecutor:
    def submit(self,fn):
        f=Future()
        try:f.set_result(fn())
        except Exception as e:f.set_exception(e)
        return f
    def shutdown(self,wait=True):pass

def test_http_api_authentication_create_and_stale_answer(tmp_path):
    jobs=IntegrationJobs(tmp_path/'jobs',driver_factory=StubDriver)
    api=IntegrationAPI(jobs,lambda:ChatCompletionsPlanner({}),'local-secret',executor=ImmediateExecutor())
    assert api.dispatch('POST','/v1/integrations',{'url':'https://example.com'})[0]==401
    assert api.dispatch('GET','/health',authorization='Bearer local-secret',origin='https://evil.example')[0]==403
    code,created=api.dispatch('POST','/v1/integrations',{'url':'https://example.com'},authorization='Bearer local-secret')
    assert code==202
    _,job=api.dispatch('GET','/v1/integrations/'+created['id'],authorization='Bearer local-secret')
    assert job['status']=='waiting_user'
    code,_=api.dispatch('GET','/v1/integrations/'+created['id']+'/mcp-config',authorization='Bearer local-secret');assert code==409
    assert api.dispatch('POST','/v1/integrations/'+created['id']+'/responses',{'question_id':'stale','answer':'yes'},authorization='Bearer local-secret')[0]==400


def test_real_local_http_api_round_trip(tmp_path):
    jobs=IntegrationJobs(tmp_path/'jobs',driver_factory=StubDriver)
    api=IntegrationAPI(jobs,lambda:ChatCompletionsPlanner({}),'local-secret')
    server=make_server(api,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',headers={'Authorization':'Bearer local-secret'}) as http:
            response=http.post('/v1/integrations',json={'url':'https://example.com'});assert response.status_code==202
            ident=response.json()['id']
            for future in api.futures:future.result(timeout=3)
            job=http.get('/v1/integrations/'+ident).json()
            assert job['question']['reason']=='model_not_configured'
    finally:server.shutdown();server.server_close();thread.join();api.close()


def test_scripted_autopilot_to_real_http_and_official_mcp(site,tmp_path):
    source,state,pack=site;origin=load_json(source/'manifest.json')['allowed_origins'][0]
    policy=load_json(EXAMPLES/(pack['name'].split('_')[0]+'.policy.json'))
    class CapturedFixtureDriver(ExplorerDriver):
        def __init__(self,root):
            super().__init__(root)
            save_private(self.exploration/'capture.json',load_json(source.parent/'capture.json'))
        def call(self,name,args):
            if name=='observe':return {'snapshot':'Fixture capture supplied; browser not used in this test'}
            return super().call(name,args)
    jobs=IntegrationJobs(tmp_path/'jobs',driver_factory=CapturedFixtureDriver)
    ident=jobs.create({'url':origin,'goal':'接入读取与审批预检'})['id']
    broken=copy.deepcopy(pack);broken['trials'][pack['reads'][0]['name']][0]['assertions']=[]
    planner=ScriptedPlanner([
        action('observe'),action('propose',policy=policy),action('candidates'),
        action('trial_pack',pack=broken),action('trial_pack',pack=pack),action('install_pack',pack=pack),
        action('tools'),action('call_tool',name=pack['name']+'__read',arguments={'id':'102'},assertions=[{'pointer':'/total','value':41}]),
        action('finish',summary='Read verified; write preflight only',remaining=['No paid or production write test'])])
    result=jobs.run(ident,planner)
    assert result['status']=='partial' and state['writes']==0
    assert {c['status'] for c in result['result']['capabilities']}=={'read_trial_passed','write_preflight_passed'}
    assert any('error' in h for c in planner.contexts for h in c['history'])
    # Generated output is a real usable MCP server, not just a config-shaped JSON.
    config=next(iter(result['result']['mcp_config']['mcpServers'].values()))
    async def check():
        async with stdio_client(StdioServerParameters(**config)) as (r,w):
            async with ClientSession(r,w) as session:
                await session.initialize();response=await session.call_tool(pack['name']+'__read',{'id':'101'})
                assert not response.isError;assert json.loads(response.content[0].text)['total']==25
    asyncio.run(check());assert state['writes']==0


def test_model_bad_json_is_repairable_without_dispatch():
    planner=ChatCompletionsPlanner({'endpoint':'http://127.0.0.1:9999/completions','model':'test','auth':'none'},transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'choices':[{'message':{'content':'not json'}}]})))
    with pytest.raises(CallError) as error:planner.next({})
    assert not isinstance(error.value,ModelUnavailable) and error.value.reason=='model_output'


def test_scope_extension_needs_actual_capture(tmp_path):
    jobs=IntegrationJobs(tmp_path/'jobs');ident=jobs.create({'url':'https://fixture.example'})['id'];driver=ExplorerDriver(jobs.directory(ident))
    save_private(driver.exploration/'capture.json',{'exchanges':[{'request':{'method':'GET','url':'https://api.fixture.example/items'},'response':{'status':200}}]})
    assert len(driver.call('network_inventory',{})['observed_requests'])==1
    with pytest.raises(CallError):driver.call('include_observed_origin',{'origin':'https://unobserved.example'})
    driver.call('include_observed_origin',{'origin':'https://api.fixture.example'})
    assert 'https://api.fixture.example' in load_json(driver.exploration/'session.json')['origins']


def test_known_presend_validation_failure_does_not_ask_user(jobs):
    j,i,d=jobs;calls=[0]
    def fail_once(name,args):
        calls[0]+=1
        if calls[0]==1:raise CallError('Invalid selector: use CSS')
        return {'snapshot':'ok'}
    d.call=fail_once
    result=j.run(i,ScriptedPlanner([action('act',effect='navigation',label='Inspect',steps=[{'type':'click','selector':'@1','purpose':'view'}]),action('observe'),action('finish',summary='done',remaining=[])]))
    assert result['status']=='ready' and result['question'] is None


def test_write_verification_is_bound_to_the_tested_revision(jobs):
    j,i,d=jobs
    d.capabilities=lambda:[{'tool':'demo_submit','status':'write_preflight_passed','revision':d.revision}]
    r=j.run(i,ScriptedPlanner([action('call_tool',name='demo_submit',arguments={})]))
    j.answer(i,r['question']['id'],'允许这次测试',approved=True)
    class ChangedPlanner:
        def next(self,context):
            d.revision='v2'
            return action('finish',summary='new revision',remaining=[])
    r=j.run(i,ChangedPlanner())
    assert d.writes==1 and r['status']=='partial'
    assert r['result']['capabilities'][0]['status']=='write_preflight_passed'
