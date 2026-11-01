"""Disposable loopback-only HTTP -> contract -> MCP demonstration."""
import asyncio
import json
import sys
import tempfile
import threading
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from lab_product import make_server
from web2mcp import Client
from web2mcp.contracts import install_contract
from web2mcp.discover import discover
from web2mcp.storage import load_json, save_private


def capture_fixture(origin, root):
    exchanges = []
    with httpx.Client(base_url=origin, timeout=10, trust_env=False) as client:
        client.post('/api/login', json={'username':'demo', 'password':'demo'}).raise_for_status()

        def observe(method, path, body=None):
            response = client.request(method, path, **({'json':body} if body is not None else {}))
            response.raise_for_status()
            request = response.request
            exchanges.append({'request':{'method':method, 'url':str(request.url),
                'headers':dict(request.headers), 'postData':request.content.decode() or None},
                'response':{'status':response.status_code, 'body':response.text},
                'label':'Synthetic loopback demonstration'})

        observe('GET', '/api/me')
        for record in ('101','102','103'):
            observe('GET', '/api/records/'+record)
        for record in ('101','103'):
            observe('POST', '/api/records/'+record+'/decision',
                {'version':1,'decision':'approved','note':'fixture sample','budget_code':'DEMO'})
    save_private(root/'capture.json', {'format':'web2mcp-capture-v1','exchanges':exchanges,'cookies':[]})
    profile = root/'profile'
    discover(root/'capture.json', profile, [origin], {
        'GET /api/me':{'name':'me','read_only':True},
        'GET /api/records/{record_id}':{'name':'detail','read_only':True},
        'POST /api/records/{record_id}/decision':{'name':'decide','read_only':False},
    })
    manifest = load_json(profile/'manifest.json')
    for op in manifest['operations']:
        if not op['read_only']:op['internal']=True
    save_private(profile/'manifest.json',manifest)
    with Client(profile) as client:
        install_contract(client,load_json(Path(__file__).with_name('purchase_contract.json')),reviewed=True)
    return profile


async def call_mcp(profile):
    params = StdioServerParameters(command=sys.executable,args=['-m','web2mcp','serve',str(profile)])
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize()

            async def call(name,arguments):
                result = await session.call_tool(name,arguments)
                if result.isError:raise RuntimeError(result.content[0].text)
                return json.loads(result.content[0].text)

            before = await call('detail',{'path':{'record_id':'102'}})
            assert before['data']['status']=='pending'
            preview = await call('purchase_approve_prepare',{'id':'102','note':'MCP demo','budget_code':'DEMO'})
            unchanged = await call('detail',{'path':{'record_id':'102'}})
            assert unchanged['data']['status']=='pending'
            result = await call('purchase_approve_submit',{'preview_id':preview['preview_id'],'confirm':True})
            assert result['business_verified']
            after = await call('detail',{'path':{'record_id':'102'}})
            duplicate = await session.call_tool('purchase_approve_submit',{'preview_id':preview['preview_id'],'confirm':True})
            assert duplicate.isError and after['data']['status']=='approved'
            return {'transport':'MCP stdio', 'record_id':'102','preview_wrote':False,
                'before':'pending','after':'approved','readback_verified':True,'duplicate_blocked':True}


def main():
    server,state = make_server()
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='web2mcp-demo-') as temp:
            profile = capture_fixture(f'http://127.0.0.1:{server.server_port}',Path(temp))
            report = asyncio.run(call_mcp(profile))
            assert state['writes'].count('102')==1
            print(json.dumps(report,ensure_ascii=False,indent=2))
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)


if __name__=='__main__':main()
