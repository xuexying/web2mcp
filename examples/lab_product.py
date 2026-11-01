"""A disposable local product for CU discovery, using fictional records only."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

HTML = '''<!doctype html><meta charset="utf-8"><title>合同执行验证站</title>
<style>body{font:17px system-ui;max-width:780px;margin:40px auto;background:#f4f6fa;color:#152338}input,select,button,textarea{font:inherit;padding:8px;margin:6px}label{display:block}article{background:white;padding:24px;border-radius:12px}button{cursor:pointer}small{color:#576579}</style>
<h1>本地事务试验站</h1><p>仅使用虚构数据。采购申请与请假申请共用此站。</p><article id="app"></article>
<script>
const app=document.querySelector('#app');
async function api(url,options={}){let r=await fetch(url,options);let d=await r.json();if(!r.ok)throw Error(d.error);return d}
async function home(){try{let me=await api('/api/me');app.innerHTML='<p id="account">当前用户：'+me.name+'</p><label>单据类型<select name="kind"><option value="purchase">采购申请</option><option value="leave">请假申请</option></select></label><button id="load">加载单据</button><div id="list"></div>';document.querySelector('#load').onclick=list;await list()}catch(e){login()}}
function login(){app.innerHTML='<h2>登录测试账户</h2><small>测试账户 demo，测试密码 demo（非真实凭据）</small><form id="login"><label>用户名<input name="username" required></label><label>密码<input name="password" type="password" required></label><button>登录</button></form>';document.querySelector('#login').onsubmit=async e=>{e.preventDefault();await api('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(new FormData(e.target)))});await home()}}
async function list(){let kind=document.querySelector('[name=kind]').value;let r=await api('/api/records?kind='+kind);let target=document.querySelector('#list');target.innerHTML=r.items.map(x=>'<p>'+x.id+' '+x.title+' · '+x.status+' <button data-record="'+x.id+'">打开 '+x.id+'</button></p>').join('');target.querySelectorAll('button').forEach(b=>b.onclick=()=>detail(b.dataset.record))}
async function detail(id){let r=await api('/api/records/'+id);app.innerHTML='<button id="back">返回列表</button><h2>'+r.title+'</h2><p id="status">状态：'+r.status+'</p><p>编号 '+r.id+' · 版本 '+r.version+'</p><form id="decision"><label>决定<select name="decision" required><option value="approved">同意</option><option value="rejected">拒绝</option></select></label><label>说明<textarea name="note" required></textarea></label>'+(r.kind==='purchase'?'<label>预算代码<input name="budget_code" required></label>':'<label>代班人<input name="cover_person" required></label>')+'<button id="submit">提交决定</button></form>';document.querySelector('#back').onclick=home;document.querySelector('#decision').onsubmit=async e=>{e.preventDefault();let data=Object.fromEntries(new FormData(e.target));data.version=r.version;await api('/api/records/'+id+'/decision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});await detail(id)}}
home();
</script>'''


def make_server(port=0):
    state={'records':{},'writes':[],'lock':threading.Lock()}
    for ident,kind,title in [(101,'purchase','测试采购 A'),(102,'purchase','测试采购 B'),(103,'purchase','测试采购 C'),
                             (201,'leave','测试请假 A'),(202,'leave','测试请假 B')]:
        row={'id':str(ident),'kind':kind,'title':title,'status':'pending','version':1}
        if ident%2: row['memo']=None
        state['records'][str(ident)]=row
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def send(self,code,value,cookie=False):
            data=json.dumps(value,ensure_ascii=False).encode();self.send_response(code)
            self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)))
            if cookie: self.send_header('Set-Cookie','lab_session=fixture-only; HttpOnly; SameSite=Strict; Path=/api')
            self.end_headers();self.wfile.write(data)
        def auth(self): return 'lab_session=fixture-only' in self.headers.get('Cookie','')
        def do_GET(self):
            p=urlsplit(self.path)
            if p.path=='/':
                data=HTML.encode();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
            if not self.auth(): return self.send(401,{'error':'login required'})
            if p.path=='/api/me': return self.send(200,{'id':'lab-user','name':'测试用户'})
            if p.path=='/api/records':
                kind=parse_qs(p.query).get('kind',['purchase'])[0]
                return self.send(200,{'items':[dict(r) for r in state['records'].values() if r['kind']==kind]})
            if p.path.startswith('/api/records/'):
                record=state['records'].get(p.path.rsplit('/',1)[-1])
                return self.send(200,dict(record)) if record else self.send(404,{'error':'not found'})
            self.send(404,{'error':'not found'})
        def do_POST(self):
            try: body=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))))
            except ValueError: return self.send(400,{'error':'invalid JSON'})
            if self.path=='/api/login':
                return self.send(200,{'ok':True},True) if body=={'username':'demo','password':'demo'} else self.send(401,{'error':'invalid login'})
            if not self.auth(): return self.send(401,{'error':'login required'})
            parts=self.path.split('/')
            if len(parts)!=5 or parts[-1]!='decision': return self.send(404,{'error':'not found'})
            with state['lock']:
                r=state['records'].get(parts[3])
                if not r: return self.send(404,{'error':'not found'})
                field='budget_code' if r['kind']=='purchase' else 'cover_person'
                if set(body)!={'decision','note','version',field} or not body.get(field) or not body.get('note') or body.get('decision') not in ('approved','rejected'):
                    return self.send(422,{'error':'required fields invalid'})
                if body['version']!=r['version'] or r['status']!='pending': return self.send(409,{'error':'state conflict'})
                r.update(body);r['status']=body['decision'];r['version']+=1;state['writes'].append(r['id'])
                return self.send(200,{'ok':True,'id':r['id'],'version':r['version']})
    return ThreadingHTTPServer(('127.0.0.1',port),Handler),state


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=0);parser.add_argument('--state-file')
    args=parser.parse_args();server,state=make_server(args.port)
    print(json.dumps({'origin':f'http://127.0.0.1:{server.server_port}'}),flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        server.server_close()
        if args.state_file:
            from web2mcp.storage import save_private
            save_private(args.state_file,{'records':state['records'],'writes':state['writes']})
