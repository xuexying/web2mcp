"""Goal-driven exploration control plane; the connected AI supplies bounded UI actions.

No embedded model/key is assumed. Browser observations are untrusted product data,
not instructions. The host agent decides intent and reviews classifications.
"""
import json
import fcntl
import os
import re
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .client import CallError
from .discover import discover
from .storage import load_json, save_private


def start_session(directory, url, goal, origins=None, budget=40, storage_sources=None):
    root=Path(directory).resolve()
    if (root/'session.json').exists(): raise CallError('Session already exists; resume it')
    origin=f'{urlsplit(url).scheme}://{urlsplit(url).netloc}'
    origins=origins or [origin]
    if not goal.strip() or not 1 <= budget <= 100: raise CallError('Goal and bounded action budget required')
    if urlsplit(url).scheme not in ('http','https') or origin not in origins or urlsplit(url).username:
        raise CallError('Invalid start URL/scope')
    state={'version':1,'id':uuid.uuid4().hex,'url':url,'goal':goal,'origins':origins,
           'budget':budget,'actions_used':0,'round':0,'stage':'created','space_id':None}
    if storage_sources is not None:
        if (not isinstance(storage_sources, list) or len(storage_sources) > 20 or
                any(not isinstance(s, dict) or set(s) != {'id','origin','area','key'} or
                    s['origin'] != origin or s['area'] not in ('localStorage','sessionStorage') for s in storage_sources)):
            raise CallError('Storage sources must explicitly name keys on the start origin')
        state['storage_sources'] = storage_sources
    save_private(root/'session.json',state)
    return {'directory':str(root),**state,'next':'observe, then choose actions from the actual snapshot'}


def explore_round(directory, action, *, runner=subprocess.run):
    root=Path(directory).resolve()
    if not (root/'session.json').exists(): raise CallError('Create a scoped exploration session first')
    fd=os.open(root/'session.lock',os.O_RDWR|os.O_CREAT,0o600)
    with os.fdopen(fd,'r+') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise CallError('An exploration round is already running; do not overlap actions') from None
        return _explore_round(root,action,runner=runner)


def _explore_round(directory, action, *, runner):
    root=Path(directory).resolve();state=load_json(root/'session.json')
    if state['stage']=='finished': raise CallError('Exploration finished')
    kind=action.get('kind','observe')
    if kind not in ('observe','actions','handoff','resume','finish','survey','source_urls'): raise CallError('Unsupported exploration action')
    steps=action.get('steps',[])
    if steps and kind!='actions': raise CallError('Only actions rounds may contain UI steps')
    if kind=='survey':
        mp=action.get('max_pages',20)
        if not isinstance(mp,int) or not 1<=mp<=60: raise CallError('Survey max_pages must be 1-60')
    if state['stage'] in ('needs_review','login_required') and kind=='actions':
        raise CallError('Inspect or resume after the prior interruption; never replay blindly')
    if kind=='resume' and action.get('user_confirmed') is not True: raise CallError('Resume requires user confirmation')
    if kind=='actions':
        if not 1 <= len(steps) <= 8 or state['actions_used']+len(steps)>state['budget']:
            raise CallError('Exploration action budget exceeded')
        for step in steps:
            if step.get('type') not in ('click','fill','select','press','goto'): raise CallError('Unsupported UI action')
            if step.get('reviewed') is not True or not step.get('purpose'): raise CallError('Each UI action needs a reviewed purpose')
            if step['type'] in ('fill', 'press'):
                # Reject before persisting action values, including when a
                # caller mistakenly labels a login action as reviewed.
                observations = sorted((root/'rounds').glob('*.output.json'))
                fields = load_json(observations[-1]).get('fields', []) if observations else []
                if (re.search('password|passwd|passphrase|one.?time.?password', step.get('selector', ''), re.I)
                        or any(f.get('type') == 'password' for f in fields)):
                    raise CallError('Complete login in the browser yourself, then resume session relay; password input is not accepted', reason='login')
            if step['type']=='goto':
                parsed=urlsplit(step['url'])
                if f'{parsed.scheme}://{parsed.netloc}' not in state['origins']: raise CallError('Navigation outside scope')
        state['actions_used']+=len(steps)
    state['round']+=1
    number=state['round'];run_id=f'{number:03d}-{uuid.uuid4().hex[:8]}'
    payload={'root':str(root),'state':state,'action':action,'run_id':run_id}
    save_private(root/'rounds'/(run_id+'.input.json'),payload)
    bridge=(Path(__file__).parent/'ego_explorer.mjs').resolve().as_uri()
    input_path=str(root/'rounds'/(run_id+'.input.json'))
    script=root/'rounds'/(run_id+'.mjs')
    script.write_text('const { run } = await import('+json.dumps(bridge)+');\nawait run('+json.dumps(input_path)+');\n')
    script.chmod(0o600)
    state['stage']='running';save_private(root/'session.json',state)
    try:
        # Cold SPA navigation can exceed one minute; allow a bounded host override.
        # A survey visits many views in one invocation, so it needs proportionally longer.
        round_timeout=int(os.environ.get('WEB2MCP_ROUND_TIMEOUT','240'))
        if kind=='survey':
            round_timeout=max(round_timeout,min(1800,action.get('max_pages',20)*25+60))
        completed=runner(['ego-browser','nodejs','-e','await import('+json.dumps(script.as_uri())+')'],
                         capture_output=True,text=True,timeout=round_timeout)
        output=root/'rounds'/(run_id+'.output.json')
        if completed.returncode!=0 or not output.exists():
            # Keep detailed CLI diagnostics private; do not forward credential-bearing traces.
            save_private(root/'rounds'/(run_id+'.failure.json'),{'stdout':completed.stdout,'stderr':completed.stderr})
            state=load_json(root/'session.json');state['stage']='needs_review';save_private(root/'session.json',state)
            raise CallError('Browser round interrupted; inspect private round diagnostic and current page. Do not replay.',reason='browser_interrupted')
        return load_json(output)
    except subprocess.TimeoutExpired:
        state=load_json(root/'session.json');state['stage']='needs_review';save_private(root/'session.json',state)
        raise CallError('Browser round timed out; effects may be late. Inspect before continuing.',reason='browser_interrupted') from None
    except FileNotFoundError:
        state=load_json(root/'session.json');state['stage']='needs_review';save_private(root/'session.json',state)
        raise CallError('ego-browser executable or round output unavailable; inspect the local installation',reason='browser_unavailable') from None


def source_map(directory, *, max_sources=40, max_bytes=25_000_000, runner=subprocess.run):
    """Static API-map: list the app's JS (source_urls round), download it, regex-extract the
    endpoint surface. Writes source_api_map.json to the session. Downloaded JS is untrusted data,
    never executed. Returns the map (endpoints only; raw source is not retained in the result)."""
    import httpx
    from .api_extract import extract
    root=Path(directory).resolve()
    res=_explore_round(root,{'kind':'source_urls'},runner=runner)
    urls=[u for u in (res.get('js_urls') or []) if isinstance(u,str) and u.startswith(('http://','https://'))]
    # Build-tool chunk reference (e.g. Vite/webpack "index-AbC123.js") embedded in a bundle,
    # so lazily-loaded route chunks are captured deterministically, not by browser timing.
    chunk_ref=re.compile(r'(?:\./)?(?:[\w-]+/)*[\w-]+-[A-Za-z0-9_]{8}\.js')
    sources,seen,queue=[],set(),list(urls)
    from urllib.parse import urljoin
    with httpx.Client(timeout=40,follow_redirects=True) as client:
        while queue and len(sources)<max_sources:
            u=queue.pop(0)
            key=u.split('?',1)[0]
            if not u.startswith(('http://','https://')) or key in seen: continue
            seen.add(key)
            try:
                r=client.get(u)
            except httpx.HTTPError:
                continue
            if r.status_code!=200 or len(r.text)>max_bytes: continue
            sources.append({'url':u,'text':r.text})
            # Discover sibling chunks referenced in this bundle (resolve against its own URL).
            for m in chunk_ref.finditer(r.text):
                cu=urljoin(u,m.group(0))
                if cu.split('?',1)[0] not in seen: queue.append(cu)
    result=extract(sources)
    save_private(root/'source_api_map.json',result)
    return result


_API_PATH = re.compile(r'/(?:v\d[\w.]*|api|rest|graphql)(?:/|$)', re.I)


def _op_name(method, path, taken):
    # Drop the whole version segment (e.g. /v0.1/) so its ".1" fragment doesn't leak into names.
    path = re.sub(r'/v\d+(?:\.\d+)*(?=/|$)', '', path, flags=re.I)
    segs = [s for s in re.split(r'[^\w]+', path) if s]
    base = (method.lower() + "_" + "_".join(segs[-4:]))[:60] or method.lower()
    name, i = base, 2
    while name in taken:
        name = f"{base}_{i}"; i += 1
    taken.add(name)
    return name


def synthesize_profile(directory, *, api_origin=None):
    """Auto-build a serveable profile from source_api_map.json (static) cross-referenced with
    capture.json (runtime samples). De-noises to real API paths, picks the API origin, maps each
    endpoint to a manifest op (read_only by method), reuses an established credential_ref when the
    session already has one. Reads are trial-ready; writes default to preview. Writes <dir>/synth/.
    Returns a summary; nothing is sent."""
    root = Path(directory).resolve()
    amap = load_json(root/'source_api_map.json')
    state = load_json(root/'session.json')
    origins = state.get('origins', [])
    # Which origin hosts the API: prefer the one most captured exchanges used, else a non-start
    # origin, else caller-supplied / first.
    cap_path = root/'capture.json'
    captured_paths, host_hits = set(), {}
    if cap_path.exists():
        for ex in (load_json(cap_path).get('exchanges') or []):
            u = ex.get('request', {}).get('url', '')
            try:
                parts = urlsplit(u)
            except ValueError:
                continue
            captured_paths.add(parts.path.rstrip('/'))
            host_hits[f'{parts.scheme}://{parts.netloc}'] = host_hits.get(f'{parts.scheme}://{parts.netloc}', 0) + 1
    if not api_origin:
        start_origin = f"{urlsplit(state.get('url','')).scheme}://{urlsplit(state.get('url','')).netloc}"
        api_origin = (max(host_hits, key=host_hits.get) if host_hits
                      else next((o for o in origins if o != start_origin), origins[0] if origins else None))
    if not api_origin:
        raise CallError('Cannot determine API origin; pass api_origin')
    # Reuse an established credential_ref (nd_mac/cookie/etc.) from a prior draft if present.
    cred_ref = 'default'
    creds = {'auth': {}}
    draft_creds = root/'draft'/'credentials.json'
    if draft_creds.exists():
        dc = load_json(draft_creds)
        if dc.get('auth'):
            cred_ref = sorted(dc['auth'])[0]
            creds = {'auth': {cred_ref: dc['auth'][cred_ref]}}
    if not creds['auth']:
        creds = {'auth': {cred_ref: {'type': 'unsupported'}}}  # user must establish auth
    taken, ops, n_read, n_write, n_captured = set(), [], 0, 0, 0
    for e in amap.get('endpoints', []):
        path = e['path']
        if e['method'] == '?' or not _API_PATH.search(path):
            continue  # skip method-unknown and library-internal (non-API) paths
        read_only = e['kind'] == 'read'
        url = api_origin + path
        if f'{urlsplit(url).scheme}://{urlsplit(url).netloc}' not in origins:
            continue  # out of reviewed scope
        op = {'name': _op_name(e['method'], path, taken), 'method': e['method'], 'url': url,
              'body': None, 'body_type': 'json', 'credential_ref': cred_ref, 'read_only': read_only,
              'description': f"{e['method']} {path} (synthesized from source; "
                             + ("captured at runtime" if path.rstrip('/') in captured_paths else "static only, trial before trusting") + ")",
              'input_schema': {'type': 'object', 'properties': (
                  {'query': {'type': 'object'}} if read_only else {'body': {'type': 'object'}})},
              'synthesized': True, 'captured': path.rstrip('/') in captured_paths}
        ops.append(op)
        n_read += read_only; n_write += (not read_only); n_captured += op['captured']
    out = root/'synth'
    out.mkdir(exist_ok=True)
    save_private(out/'manifest.json', {'allowed_origins': origins, 'operations': ops})
    save_private(out/'credentials.json', creds)
    return {'profile': str(out), 'api_origin': api_origin, 'operations': len(ops),
            'read': n_read, 'write': n_write, 'captured': n_captured, 'credential_ref': cred_ref,
            'next': 'trial-read the reads then publish; writes stay preview until you authorize.'}


def propose(directory, policy=None, *, revision=False):
    root=Path(directory).resolve();state=load_json(root/'session.json')
    if not (root/'capture.json').exists(): raise CallError('No captured exchanges yet')
    if revision and not isinstance(policy, dict): raise CallError('A revised interface policy is required')
    if revision:
        import uuid
        destination=root/'drafts'/uuid.uuid4().hex
    else: destination=root/'draft'
    if (destination/'manifest.json').exists(): raise CallError('Draft already exists; review it instead of overwriting trials')
    result=discover(root/'capture.json',destination,state['origins'],policy)
    if policy is not None: save_private(destination/'policy.reviewed.json',policy)
    manifest=load_json(destination/'manifest.json');draft_policy={}
    for operation in manifest['operations']:
        operation['internal']=True
        operation['verification']='needs_trial'
        label=next((e['action'] for e in reversed(operation['evidence']) if e.get('action')),None)
        if label: operation['description']=label+'; captured candidate, review semantics before use.'
        key=operation['method']+' '+operation['url']
        entry={'name':operation['name'],'description':operation['description'],
                     'read_only':operation['read_only'],'classification':'candidate_requires_review'}
        if operation.get('request_body_matches'):
            entry['match']=operation['request_body_matches']
            draft_policy.setdefault(key,{'variants':[]})['variants'].append(entry)
        else: draft_policy[key]=entry
        save_private(destination/'cards'/(operation['name']+'.json'),
            {'type':'tool_trial','state':'review_required','operation':operation['name'],
             'read_only_candidate':operation['read_only'],'path_inference':operation['path_inference'],
             'sample_count':operation['samples'],'requirements':['review read/write semantics','vary arguments','assert business result'],
             'goal':state['goal']})
    save_private(destination/'manifest.json',manifest);save_private(root/'policy.draft.json',draft_policy)
    state['draft_profile']=str(destination)
    if state['stage']!='finished': state['stage']='draft_ready'
    save_private(root/'session.json',state)
    return {**result,'published':0,'policy':str(root/'policy.draft.json'),'next':'Review candidates, run read trials, then publish; writes use reviewed contracts with read-back verification.'}
