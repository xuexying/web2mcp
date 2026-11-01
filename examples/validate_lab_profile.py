"""One-shot live validation of an actual CU-generated lab profile, after browser closure.
Run only against examples/lab_product.py. Business writes use MCP, never browser APIs.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from web2mcp import Client
from web2mcp.contracts import install_contract
from web2mcp.runtime import trial_read,publish_reads
from web2mcp.storage import load_json,save_private


def prepare(profile):
    manifest=load_json(profile/'manifest.json')
    if any(urlsplit(o).hostname!='127.0.0.1' for o in manifest['allowed_origins']):
        raise RuntimeError('Lab validation only permits loopback profiles')
    names={}
    for op in manifest['operations']:
        p=urlsplit(op['url']).path
        label={'/api/me':'me','/api/records':'list','/api/records/{record_id}':'detail','/api/records/{record_id}/decision':'decide'}.get(p)
        if label:names[label]=op['name']
    assert set(names)=={'me','list','detail','decide'}
    trials=[]
    with Client(profile) as client:
        trials.append(trial_read(client,names['me'],[{'assertions':[{'pointer':'/data/id','value':'lab-user'}]}],reviewed_read_only=True))
        trials.append(trial_read(client,names['list'],[
            {'arguments':{'query':{'kind':kind}},'assertions':[{'pointer':'/data/items/0/kind','value':kind},{'pointer':'/data/items/'+str(last)+'/id','value':ident}]}
            for kind,last,ident in [('purchase',2,'103'),('leave',1,'202')]],reviewed_read_only=True))
        trials.append(trial_read(client,names['detail'],[
            {'arguments':{'path':{'record_id':ident}},'assertions':[{'pointer':'/data/id','value':ident},{'pointer':'/data/status','value':'pending'}]}
            for ident in ('102','202')],reviewed_read_only=True))
        publish_reads(client,[names[n] for n in ('me','list','detail')])
    # Review contracts against the form fields and two captured write payloads.
    with Client(profile) as client:
        for kind in ('purchase','leave'):
            contract=load_json(Path(__file__).parent/(kind+'_contract.json'))
            for step in contract['preflight']+contract['verify']:step['operation']=names['detail']
            contract['write']['operation']=names['decide']
            install_contract(client,contract,reviewed=True)
    save_private(profile/'runtime.json',{'identity_probe':{'operation':names['me'],'pointer':'/data/id','expected':'lab-user'}})
    return names,trials


async def validate(profile,names):
    results=[]
    params=StdioServerParameters(command=sys.executable,args=[str(profile/'server.py')])
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize();available=await session.list_tools()
            assert names['decide'] not in {t.name for t in available.tools}
            async def call(name,args):
                response=await session.call_tool(name,args)
                if response.isError:raise RuntimeError(response.content[0].text)
                return json.loads(response.content[0].text)
            assert (await call('runtime_health',{}))['healthy']
            for kind,ident,field,value in [('purchase','102','budget_code','MCP-LAB-2026'),('leave','202','cover_person','MCP测试代班人')]:
                args={'id':ident,'note':'关闭浏览器后通过 MCP 审批验证',field:value}
                before=await call(names['detail'],{'path':{'record_id':ident}})
                preview=await call(kind+'_approve_prepare',args)
                still=await call(names['detail'],{'path':{'record_id':ident}})
                assert still['data']['status']=='pending'
                result=await call(kind+'_approve_submit',{'preview_id':preview['preview_id'],'confirm':True})
                assert result['business_verified']
                after=await call(names['detail'],{'path':{'record_id':ident}})
                assert after['data']['id']==ident and after['data']['status']=='approved' and after['data'][field]==value
                repeated=await session.call_tool(kind+'_approve_submit',{'preview_id':preview['preview_id'],'confirm':True})
                assert repeated.isError
                results.append({'kind':kind,'record_id':ident,'before_status':before['data']['status'],
                                'preview_wrote':False,'after_status':after['data']['status'],'version':after['data']['version'],
                                'business_verified':True,'repeat_blocked':True,'field':field})
            audit=await call('runtime_audit_verify',{})
    return {'tools':len(available.tools),'business_results':results,'audit':audit}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('profile');parser.add_argument('--report',required=True)
    args=parser.parse_args();profile=Path(args.profile).resolve()
    session=load_json(profile.parent/'session.json')
    assert session['stage']=='finished','Close the CU exploration space before browser-free validation'
    names,trials=prepare(profile);result=asyncio.run(validate(profile,names))
    result.update({'browser_finished_before_mcp':True,'capture_exchanges':len(load_json(profile.parent/'capture.json')['exchanges']),
                   'generated_operations':len(names),'read_trials':len(trials),'trial_cases':sum(len(t['cases']) for t in trials),
                   'cu_actions':session['actions_used'],'production_writes':0})
    save_private(args.report,result)
    print(json.dumps(result,ensure_ascii=False,indent=2))
