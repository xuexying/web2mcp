"""Collector regression: responseReceived is not yet a readable response body."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize('completion', ['Network.loadingFinished', 'Network.loadingFailed'])
def test_capture_drains_completion_without_replaying_request(tmp_path, completion):
    node = shutil.which('node')
    if not node: pytest.skip('Node is required for the browser collector test')
    module = Path(__file__).resolve().parents[1]/'web2mcp'/'ego_capture.mjs'
    script = tmp_path/'test.mjs'
    script.write_text('''
const {saveCapture} = await import(process.argv[2]);
const completion = process.argv[4];
let rounds = 0, completed = false;
const calls = [];
const page = {
  async events() {
    rounds++;
    if (rounds === 1) return [
      {method:'Network.requestWillBeSent',params:{requestId:'one',type:'Fetch',request:{method:'GET',url:'https://fixture.example/tasks',headers:{}}}},
      {method:'Network.responseReceived',params:{requestId:'one',type:'Fetch',response:{status:200,mimeType:'application/json',headers:{}}}}
    ];
    completed = completion === 'Network.loadingFinished';
    return [{method:completion,params:{requestId:'one'}}];
  },
  async cdp(method, parameters) {
    calls.push(method);
    if (method === 'Network.getCookies') return {cookies:[]};
    if (method !== 'Network.getResponseBody') throw new Error('Unexpected command');
    if (!completed) throw new Error('Body not available');
    return {body:'{"items":[{"id":"different-record"}]}',base64Encoded:false};
  }
};
const result = await saveCapture(page,process.argv[3],'fixture capture');
console.log(JSON.stringify({result,rounds,calls}));
''')
    completed = subprocess.run([node,str(script),module.as_uri(),str(tmp_path/'capture'),completion],capture_output=True,text=True,check=True,timeout=5)
    result = json.loads(completed.stdout)
    data = json.loads((tmp_path/'capture'/'capture.json').read_text())
    response = data['exchanges'][0]['response']
    assert result['rounds'] == 2
    assert result['calls'] == ['Network.getResponseBody','Network.getCookies']
    if completion == 'Network.loadingFinished':
        assert json.loads(response['body'])['items'][0]['id'] == 'different-record'
        assert response['unavailable'] is None
    else:
        assert response['body'] is None and response['unavailable']


@pytest.mark.parametrize('fallback', [False, True])
def test_authoritative_session_snapshot_and_password_exclusion(tmp_path, fallback):
    node=shutil.which('node')
    if not node:pytest.skip('Node required')
    module=Path(__file__).resolve().parents[1]/'web2mcp/ego_capture.mjs'
    script=tmp_path/'relay.mjs'
    script.write_text('''
import fs from 'node:fs/promises';
const {saveCapture}=await import(process.argv[2]);
const dir=process.argv[3],fallback=process.argv[4]==='true';
await fs.mkdir(dir);
await fs.writeFile(dir+'/capture.json',JSON.stringify({format:'web2mcp-capture-v1',exchanges:[],cookies:[{name:'deleted',value:'old',domain:'.fixture.example',path:'/'}]}));
const cookies=[{name:'sid',value:'private-session',domain:'.fixture.example',path:'/',httpOnly:true},
 {name:'other',value:'excluded-secret',domain:'.unrelated.example',path:'/'}];
const requests=[
 {method:'GET',url:'https://fixture.example/api/items',headers:{}},
 {method:'POST',url:'https://fixture.example/api/auth',headers:{},postData:'{"user":{"newPassword":"must-not-save"}}'},
 {method:'POST',url:'https://fixture.example/api/auth',headers:{},postData:'data=%7B%22password%22%3A%22must-not-save%22%7D'},
 {method:'GET',url:'https://fixture.example/api/auth',headers:{Authorization:'Basic must-not-save'}},
 {method:'GET',url:'https://unrelated.example/data',headers:{}}];
const events=requests.flatMap((request,i)=>[
 {method:'Network.requestWillBeSent',params:{requestId:String(i),type:'Fetch',request}},
 {method:'Network.requestWillBeSentExtraInfo',params:{requestId:String(i),headers:i===0?{Cookie:'sid=private-session','X-Auth':'private-header'}:{}}},
 {method:'Network.responseReceived',params:{requestId:String(i),type:'Fetch',response:{status:200,headers:{},mimeType:'application/json'}}},
 {method:'Network.loadingFinished',params:{requestId:String(i)}}]);
const calls=[];
const page={events:async()=>events,cdp:async(method,args)=>{
 calls.push({method,args});
 if(method==='Network.getResponseBody')return {body:'{"ok":true}'};
 if(method==='Network.getAllCookies'&&fallback)throw Error('unsupported');
 return {cookies};
}};
const result=await saveCapture(page,dir,'fixture',{origins:['https://fixture.example']});
console.log(JSON.stringify({result,calls}));
''')
    done=subprocess.run([node,str(script),module.as_uri(),str(tmp_path/'capture'),str(fallback).lower()],capture_output=True,text=True,check=True,timeout=5)
    result=json.loads(done.stdout)
    path=tmp_path/'capture/capture.json';raw=path.read_text();data=json.loads(raw)
    assert len(data['exchanges'])==1
    assert data['exchanges'][0]['request']['headers']['X-Auth']=='private-header'
    assert 'must-not-save' not in raw and 'excluded-secret' not in raw and 'deleted' not in raw
    assert [c['name'] for c in data['cookies']]==['sid']
    snapshot=data['sessions']['https://fixture.example']
    assert snapshot['captured_at'] and snapshot['cookies'][0]['hostOnly'] is False
    assert path.stat().st_mode & 0o777==0o600
    assert 'private-session' not in json.dumps(result['result'])
    assert len([c for c in result['calls'] if c['method']=='Network.getResponseBody'])==1
