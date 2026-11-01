"""Real local HTTP + official MCP stdio: three incompatible wire formats, one engine."""
import asyncio
import copy
import json
import sys
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from web2mcp.client import Client, CallError
from web2mcp.contracts import ContractEngine
from web2mcp.discover import discover
from web2mcp.expressions import bind, validate_template
from web2mcp.packs import PackReader, compile_pack, draft_pack, install_pack, trial_pack
from web2mcp.storage import load_json, save_private
from web2mcp import Product

EXAMPLES = Path(__file__).resolve().parents[1]/'examples'/'packs'


@pytest.fixture(params=['rest', 'rpc', 'form'])
def site(request, tmp_path):
    style = request.param
    state = {'writes': 0, 'csrf': 0, 'requests': 0, 'rows': {}}
    for ident in ('100', '101', '102', '103'):
        state['rows'][ident] = {'id': ident, 'title': 'record '+ident, 'status': 'pending', 'version': 1,
            'lines': [{'quantity': 2, 'price': 10 if ident != '102' else 18}, {'quantity': 1, 'price': 5}]}

    def wire(row):
        row = copy.deepcopy(row)
        if style == 'rest': return {'ticket': row}
        if style == 'rpc':
            for a, b in [('id', 'key'), ('status', 'phase'), ('version', 'revision'), ('lines', 'items')]: row[b] = row.pop(a)
            return {'success': True, 'result': row}
        for a, b in [('id', 'documentId'), ('status', 'state'), ('version', 'revision'), ('lines', 'rows')]: row[b] = row.pop(a)
        return {'document': {'fields': row}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def respond(self, code, payload, rotate=False):
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            if rotate:
                state['csrf'] += 1
                self.send_header('Set-Cookie', 'XSRF-TOKEN=rotated-'+str(state['csrf'])+'; Path=/')
            self.end_headers(); self.wfile.write(json.dumps(payload).encode())
        def handle_api(self):
            state['requests'] += 1
            path = urlsplit(self.path).path
            body = self.rfile.read(int(self.headers.get('content-length', '0')))
            data = json.loads(body) if body and style != 'form' else {k:v[0] for k,v in parse_qs(body.decode()).items()}
            if style == 'rest':
                if self.headers.get('Authorization') != 'Bearer test-only': return self.respond(401, {})
                ident = path.split('/')[3]; write = self.command == 'POST'; values = data
            elif style == 'rpc':
                if self.headers.get('X-Tenant-Key') != 'lab-only': return self.respond(401, {})
                ident = data['params']['key']; write = data['method'] == 'task.decide'; values = data['params']
            else:
                cookie = SimpleCookie(); cookie.load(self.headers.get('cookie', ''))
                if 'session' not in cookie or cookie['session'].value != 'test-only': return self.respond(401, {})
                write = self.command == 'POST'
                ident = data['documentId'] if write else parse_qs(urlsplit(self.path).query)['id'][0]
                values = json.loads(data['payload']) if write else {}
                if write and self.headers.get('X-XSRF-TOKEN') != 'rotated-'+str(state['csrf']): return self.respond(403, {})
            row = state['rows'][ident]
            if write:
                if row['version'] != values['version']: return self.respond(409, {'conflict': True})
                state['writes'] += 1
                for k in ('note', 'due', 'total'): row[k] = values[k]
                row['status'] = values['decision']; row['version'] += 1
            self.respond(200, wire(row), rotate=style == 'form' and not write)
        do_GET = handle_api
        do_POST = handle_api

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    origin = f'http://127.0.0.1:{server.server_port}'
    try:
        # Capture actual HTTP fixture requests, NOT synthetic browser exploration.
        exchanges = []
        with httpx.Client(base_url=origin) as http:
            if style == 'form': http.cookies.set('session', 'test-only', domain='127.0.0.1', path='/')
            def observe(method, path, **kwargs):
                response = http.request(method, path, **kwargs); response.raise_for_status()
                req = response.request
                exchanges.append({'request': {'method': req.method, 'url': str(req.url), 'headers': dict(req.headers), 'postData': req.content.decode()},
                                  'response': {'status': response.status_code, 'body': response.text}})
                return response
            for ident in ('101', '102', '100', '103'):
                if style == 'rest': observe('GET', '/rest/tickets/'+ident, headers={'Authorization': 'Bearer test-only'})
                elif style == 'rpc': observe('POST', '/rpc', json={'method':'task.get','params':{'key':ident}}, headers={'X-Tenant-Key':'lab-only'})
                else: observe('GET', '/forms/load', params={'id':ident})
                if ident not in ('100', '103'): continue
                values = {'version':1,'decision':'approved','note':'capture sample','due':'2026-11-22','total':25.0}
                if style == 'rest': observe('POST','/rest/tickets/'+ident+'/decision',json=values,headers={'Authorization':'Bearer test-only'})
                elif style == 'rpc': observe('POST','/rpc',json={'method':'task.decide','params':{'key':ident,**values}},headers={'X-Tenant-Key':'lab-only'})
                else: observe('POST','/forms/save',data={'documentId':ident,'payload':json.dumps(values)},headers={'X-XSRF-TOKEN':'rotated-'+str(state['csrf'])})
            cookies = [{'name': c.name,'value':c.value,'domain':c.domain,'path':c.path,'secure':c.secure} for c in http.cookies.jar]
        capture = tmp_path/'capture.json'
        save_private(capture, {'format':'web2mcp-capture-v1','exchanges':exchanges,'cookies':cookies})
        root = tmp_path/'profile'
        discover(capture,root,[origin],load_json(EXAMPLES/(style+'.policy.json')))
        manifest = load_json(root/'manifest.json')
        for op in manifest['operations']: op['internal'] = True
        save_private(root/'manifest.json',manifest)
        state['writes'] = 0
        yield root, state, load_json(EXAMPLES/(style+'.pack.json'))
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_three_sites_via_official_mcp_without_browser(site):
    root, state, pack = site
    with Client(root) as client:
        receipt = trial_pack(client, pack)
        assert receipt['business_writes_sent'] == state['writes'] == 0
        assert sum(r['cases'] for r in receipt['results']) == 4
        installed = install_pack(client, pack, reviewed=True)
    async def run():
        async with stdio_client(StdioServerParameters(command=sys.executable,args=[str(root/'server.py')])) as (read, write):
            async with ClientSession(read,write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert set(t.name for t in tools.tools) == set(installed['tools'])
                async def call(name, arguments):
                    result = await session.call_tool(pack['name']+'__'+name, arguments)
                    assert not result.isError, result.content
                    return json.loads(result.content[0].text)
                for ident, total in [('101', 25), ('102', 41)]:
                    result = await call('read', {'id':ident})
                    assert result == {'id':ident,'status':'pending','total':total}
                for index, ident in enumerate(('101', '102')):
                    preview = await call('approve_prepare', {'id':ident,'note':'MCP approval '+ident,'start':'2026-10-23'})
                    assert preview['ready'] and state['writes'] == index
                    result = await call('approve_submit', {'preview_id':preview['preview_id'],'confirm':True})
                    assert result['business_verified'] and state['writes'] == index+1
                    status = await call('approve_status', {'preview_id':preview['preview_id']})
                    assert status['state'] == 'verified'
                    assert (await call('read', {'id':ident}))['status'] == 'approved'
                assert state['rows']['102']['due'] == '2026-11-22'
                assert state['rows']['102']['total'] == 41
    asyncio.run(run())
    with Client(root) as client: assert client.audit.verify()['valid']


def test_pack_requires_unchanged_trials_and_review(site):
    root, state, pack = site
    with Client(root) as client:
        with pytest.raises(CallError, match='Review'): install_pack(client, pack)
        with pytest.raises(CallError, match='trial-pack'): install_pack(client, pack, reviewed=True)
        trial_pack(client, pack)
        changed = copy.deepcopy(pack); changed['description'] += ' changed'
        with pytest.raises(CallError, match='changed'): install_pack(client, changed, reviewed=True)
        install_pack(client, pack, reviewed=True)
        reader = PackReader(client); engine = ContractEngine(client)
        preview = engine.call(pack['name']+'__approve_prepare', {'id':'101','note':'test','start':'2026-10-23'})
        state['rows']['101']['version'] += 1
        with pytest.raises(CallError, match='state changed'): engine.submit(pack['name']+'__approve',preview['preview_id'])
        assert state['writes'] == 0
        client.operations['detail']['description'] += ' changed'
        with pytest.raises(CallError, match='changed'): reader.call(pack['name']+'__read', {'id':'101'})
        with pytest.raises(CallError, match='changed'): ContractEngine(client)


def test_read_pipeline_and_output_validation(site):
    root, state, pack = site
    with Client(root) as client:
        read = pack['reads'][0]
        second = copy.deepcopy(read['steps'][0]); second['id'] = 'second'
        # Second operation receives the actual key returned by the first.
        first_output = read['output']['id']
        def substitute(value):
            if value == {'$arg':'/id'}: return copy.deepcopy(first_output)
            if isinstance(value, dict): return {k:substitute(v) for k,v in value.items()}
            if isinstance(value, list): return [substitute(v) for v in value]
            return value
        second['arguments'] = substitute(second['arguments'])
        read['steps'].append(second)
        trial_pack(client,pack);install_pack(client,pack,reviewed=True)
        result = PackReader(client).call(pack['name']+'__read', {'id':'102'})
        assert result['total'] == 41 and state['writes'] == 0
        broken = copy.deepcopy(pack)
        broken['reads'][0]['output_schema']['properties']['total'] = {'type':'string'}
        with pytest.raises(CallError, match='Recipe value invalid'): trial_pack(client, broken)


def test_pack_draft_and_static_guards(site):
    root, state, pack = site
    with Client(root) as client:
        draft = draft_pack(client, 'other_site')
        assert draft['bindings'] and draft['reads'] and not draft['contracts']
        compile_pack(client,draft)
        with pytest.raises(CallError, match='trial cases'): trial_pack(client,draft)
        for mutate, message in [
            (lambda p: p['reads'][0]['steps'][0].update(operation='decide'), 'classification'),
            (lambda p: p['reads'][0].update(output={'$step':'missing','pointer':'/data'}), 'forward'),
            (lambda p: p['reads'][0].update(output={'$fn':'eval','args':['1+1']}), 'Unsupported'),
            (lambda p: p['contracts'][0]['write'].update(allowed_body_fields=[]), 'allowed_body_fields'),
            (lambda p: p['contracts'][0]['write'].update(arguments={'body':{'$literal':{'unexpected':1}}},allowed_body_fields=['$literal']), 'literal'),
            (lambda p: p['contracts'][0]['preflight'][0].update(id='write'), 'reserved'),
        ]:
            changed = copy.deepcopy(pack); mutate(changed)
            before = state['requests']
            with pytest.raises(CallError,match=message): compile_pack(client,changed)
            assert state['requests'] == before
        if pack['name'] == 'rpc_demo':
            before = state['writes']
            with pytest.raises(CallError,match='discriminator'):
                client.call('detail',{'body':{'method':'task.decide','params':{'key':'101'}}})
            assert state['writes'] == before
            schema = load_json(root/'openapi.json')['paths']['/rpc']['post']
            assert len(schema['x-web2mcp-variants']) == 2


def test_public_python_product_and_pack_replacement(site):
    root, state, pack = site
    with Client(root) as client:
        trial_pack(client,pack);install_pack(client,pack,reviewed=True)
    with Product(root) as product:
        assert product.call(pack['name']+'__read', {'id':'101'})['total'] == 25
        with pytest.raises(CallError,match='not published'): product.call('detail',{})
        preview = product.call(pack['name']+'__approve_prepare', {'id':'101','note':'SDK','start':'2026-10-23'})
        changed = copy.deepcopy(pack); changed['description'] += ' new revision'
        with Client(root) as client:
            trial_pack(client,changed);install_pack(client,changed,reviewed=True)
        with pytest.raises(CallError,match='changed'): product.call(pack['name']+'__read', {'id':'101'})
        with pytest.raises(CallError,match='changed'):
            product.call(pack['name']+'__approve_submit', {'preview_id':preview['preview_id'],'confirm':True})
    assert state['writes'] == 0


def test_pack_cli_and_explorer_entry_points(site, monkeypatch, capsys):
    from web2mcp.cli import main
    from web2mcp.explorer_server import definitions, execute
    root, state, pack = site
    pack_file = root/'source-pack.json'; save_private(pack_file,pack)
    for command in ('check-pack','trial-pack','install-pack'):
        argv = ['web2mcp',command,str(root),str(pack_file)]
        if command == 'install-pack': argv.append('--reviewed')
        monkeypatch.setattr(sys,'argv',argv); main()
        assert json.loads(capsys.readouterr().out)
    monkeypatch.setattr(sys,'argv',['web2mcp','call',str(root),pack['name']+'__read','--args','{"id":"102"}'])
    main(); assert json.loads(capsys.readouterr().out)['total'] == 41
    assert {'explorer_draft_pack','explorer_trial_pack','explorer_install_pack'} <= {n for n,_,_,_ in definitions()}
    assert state['writes'] == 0


def test_explorer_can_revise_policy_without_overwriting_profiles(site):
    from web2mcp.explorer_server import execute
    root, state, pack = site
    session = root.parent
    save_private(session/'session.json', {'origins':load_json(root/'manifest.json')['allowed_origins'],
        'goal':'Read and approve fixture records', 'stage':'finished'})
    first = execute(session,'explorer_propose',{})
    initial_manifest = load_json(Path(first['profile'])/'manifest.json')
    revised = execute(session,'explorer_revise_policy',{'policy':load_json(EXAMPLES/(pack['name'].replace('_demo','')+'.policy.json'))})
    assert first['profile'] != revised['profile']
    assert load_json(Path(first['profile'])/'manifest.json') == initial_manifest
    manifest = load_json(Path(revised['profile'])/'manifest.json')
    assert len(manifest['operations']) == 2 and all(o['internal'] for o in manifest['operations'])
    assert execute(session,'explorer_draft_pack',{'name':'new_site'})['bindings']
    assert execute(session,'explorer_trial_pack',{'pack':pack})['passed']
    assert execute(session,'explorer_install_pack',{'pack':pack,'reviewed':True})['reconnect_required']
    assert state['writes'] == 0


def test_csrf_never_falls_back_to_captured_header(site):
    root, state, pack = site
    if pack['name'] != 'form_demo': return
    with Client(root) as client:
        client.http.cookies.clear()
        with pytest.raises(CallError,match='CSRF cookie missing'):
            client.call('decide', {'body':{'documentId':'101','payload':'{}'}},confirm=True)
        client.http.cookies.set('XSRF-TOKEN','foreign',domain='unrelated.example',path='/')
        with pytest.raises(CallError,match='CSRF cookie missing'):
            client.call('decide', {'body':{'documentId':'101','payload':'{}'}},confirm=True)
    assert state['writes'] == 0


@pytest.mark.parametrize('template,expected', [
    ({'$fn':'date_add_days','args':['2026-10-23',30]}, '2026-11-22'),
    ({'$get':{'$arg':''},'pointer':'/missing','default':'fallback'}, 'fallback'),
    ({'$if':{'$fn':'eq','args':[1,1]},'then':'yes','else':{'$arg':'/missing'}}, 'yes'),
    ({'$literal':{'$system':'ordinary data'}}, {'$system':'ordinary data'}),
    ({'$fn':'sum','args':[{'$map':[{'n':'2'},{'n':'3.5'}],'value':{'$fn':'number','args':[{'$item':'/n'}]}}]},5.5),
])
def test_bounded_mapping_language(template, expected):
    assert bind(template,{}, {}) == expected


@pytest.mark.parametrize('template', [
    {'$fn':'eval','args':['print(1)']},
    {'$map':list(range(1001)),'value':{'$item':''}},
    {'$fn':'number','args':['nan']},
    {'$fn':'date_add_days','args':['bad-date',30]},
    {'$item':'/n'},
    {'$if':'truthy','then':1,'else':2},
    {'$arg':'/missing'},
])
def test_mapping_errors_are_explicit(template):
    with pytest.raises(CallError): bind(template, {}, {})
